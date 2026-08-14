import threading

from contextlib import contextmanager
from contextvars import ContextVar

from winslow.exceptions import InitializationError
from winslow.util import execute_in_threads
from winslow.cache.base import eager_fields
from winslow.cache.container import CacheContainer
from winslow.cache.log import eager_population_scope


# The process-level state of the global scope: the orchestrator stores the
# collected registry, the first workflow initialization builds the container.
_global_registry = None
_global_container = None
_global_populated = False
# Two locks: the container build must never wait behind a slow eager loader,
# so the population runs under its own lock.
_global_lock = threading.Lock()
_populate_lock = threading.Lock()


def set_global_cache_registry(registry):
    """Store the collected GlobalCacheRegistry for this process."""
    global _global_registry
    _global_registry = registry


def initialize_global_cache(
    orchestrator_config, disable_concurrency=False, clear=False
):
    """Build and populate the process container once; the first caller's config
    wins and a failed population retries later. `clear` drops every entry first."""
    global _global_container, _global_populated
    with _global_lock:
        if _global_container is None:
            classes = _global_registry.classes if _global_registry else ()
            _global_container = CacheContainer(
                {kls.get_name(): kls(orchestrator_config) for kls in classes}
            )
        container = _global_container
    with _populate_lock:
        # reset_global_cache can run between the two locks. A container that
        # is no longer the global one must not populate or mark the flag.
        if _global_container is container:
            if clear:
                for cache in container._instances.values():
                    cache.invalidate_all()
            if clear or not _global_populated:
                populate_eager_entries(
                    container._instances.values(), disable_concurrency
                )
                _global_populated = True
    return container


def get_global_cache():
    """The process-level container. It exists after the first workflow
    initialization."""
    if _global_container is None:
        raise RuntimeError(
            "The global cache container does not exist yet - it is built at "
            "the first workflow initialization."
        )
    return _global_container


def reset_global_cache():
    """For tests only. Drop the process-level container, its registry and the
    populated flag."""
    global _global_registry, _global_container, _global_populated
    with _global_lock, _populate_lock:
        _global_registry = None
        _global_container = None
        _global_populated = False


# The active workflow container, for the classmethod hooks that run before a
# task instance exists. A batch thread reads the stamp on the task instead.
_workflow_container: ContextVar = ContextVar("workflow_cache_container", default=None)


def get_workflow_cache():
    """The workflow container of the current context."""
    container = _workflow_container.get()
    if container is None:
        raise RuntimeError(
            "No active workflow cache container in this context - it is "
            "available during the workflow initialization and on the tasks."
        )
    return container


@contextmanager
def workflow_cache_context(container):
    token = _workflow_container.set(container)
    try:
        yield container
    finally:
        _workflow_container.reset(token)


def populate_eager_entries(caches, disable_concurrency=False):
    """Touch every eager field from one flat pool. No ordering machinery: a
    dependent's loader blocks on the field lock of the entry it reads."""
    jobs = [(cache, name) for cache in caches for name in eager_fields(type(cache))]
    # disable_concurrency serializes the pool, as it does for tasks.
    execute_in_threads(
        _populate_entry, jobs, max_workers=1 if disable_concurrency else None
    )


def _populate_entry(cache, name):
    """Load one eager field. An exception aborts the workflow initialization
    loudly; the failed field stays cold, so a later initialization retries it."""
    with eager_population_scope():
        try:
            getattr(cache, name)
        except Exception as exc:
            raise InitializationError(
                f"Eager cache entry '{cache}.{name}' failed to load: {exc}"
            ) from exc
