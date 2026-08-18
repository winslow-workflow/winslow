import logging

from contextlib import contextmanager
from contextvars import ContextVar

from winslow.logger import RUNS_LOGGER_NAME, TASK_LOGGER_NAME, run_logger_name


# The producer for a cache emission outside every task and session scope. It
# propagates to the winslow.runs sinks (see ContextStampFilter).
CACHE_LOGGER_NAME = f"{RUNS_LOGGER_NAME}.cache"


def _log_context():
    # A lazy import, so a cache import does not import the task package
    # (compare ContextStampFilter).
    from winslow.task.context import get_log_context

    return get_log_context()


def cache_logger():
    """The logger for one cache emission, resolved from the ambient log context.
    A cache never stores a logger: a GlobalCache outlives every session."""
    ctx = _log_context()
    if ctx is not None and ctx.task_uuid is not None:
        return logging.LoggerAdapter(
            logging.getLogger(TASK_LOGGER_NAME), {"task_id": ctx.task_uuid}
        )
    if ctx is not None and ctx.session_id is not None:
        # A session-scoped emission outside a task, for example a UI action or
        # the eager population: the session logger feeds the session's sinks.
        return logging.getLogger(run_logger_name(ctx.session_id))
    return logging.getLogger(CACHE_LOGGER_NAME)


# True while an eager population job runs. emit_lazy_error reads it, so an
# eager loader error is reported only by the initialization boundary.
_eager_population: ContextVar = ContextVar("cache_eager_population", default=False)


def is_eager_population():
    return _eager_population.get()


@contextmanager
def eager_population_scope():
    token = _eager_population.set(True)
    try:
        yield
    finally:
        _eager_population.reset(token)


def emit_lazy_error(cache, name, exc):
    """Emit a loader error that no other boundary covers: a task step and an
    eager load have their own boundaries (see telemetry.py, one emission)."""
    from winslow.task.context import get_execution_context
    from winslow.telemetry import emit_unscoped_error

    if get_execution_context() is not None or is_eager_population():
        return
    ctx = _log_context()
    emit_unscoped_error(
        exc,
        workflow_name=getattr(ctx, "workflow_name", None),
        session_id=getattr(ctx, "session_id", None),
        workflow_instance=getattr(ctx, "workflow_instance", None),
    )
