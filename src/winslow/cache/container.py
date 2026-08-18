from contextlib import contextmanager

from winslow.exceptions import MisconfigurationError
from winslow.util import ListenerMixin
from winslow.cache.base import eager_fields
from winslow.cache.listener import CacheListener


# The trigger label of a container-wide drop (see on_entries_invalidated).
CLEAR_ALL_TRIGGER = "clear_all"


class CacheContainer(ListenerMixin):
    """A read-only namespace of cache instances, by cache name. A cache name
    must not clash with the container API; the constructor validates it.

    The container is also the event hub of its scope: a cache emits through
    it, and a UI adapter subscribes to it (see CacheListener)."""

    def __init__(self, instances, scope=None):
        instances = dict(instances)
        # Normal attribute lookup wins over __getattr__, so a cache whose
        # name matches a container member would be unreachable silently.
        if clashes := sorted(name for name in instances if hasattr(type(self), name)):
            raise MisconfigurationError(
                f"Cache name(s) {', '.join(map(repr, clashes))} clash with the "
                f"cache container API - choose another name."
            )
        object.__setattr__(self, "_instances", instances)
        # The scope label for the events. The caches know their scope, so a
        # missing argument derives from the first instance.
        if scope is None and instances:
            scope = next(iter(instances.values())).scope
        object.__setattr__(self, "_scope", scope)
        self._init_listeners()
        for cache in instances.values():
            cache._attach_container(self)

    def caches(self):
        """The cache instances of the container, in name order."""
        return tuple(self._instances[name] for name in sorted(self._instances))

    def inspect(self):
        """One CacheEntryInfo per entry of every cache of the container."""
        return tuple(info for cache in self.caches() for info in cache.inspect())

    def clear_all(self):
        """Drop every entry of every cache and emit one multi-cache event with
        the live drops (see CacheListener.on_entries_invalidated)."""
        dropped = {
            name: drops
            for name, cache in self._instances.items()
            if (drops := cache._drop_all())
        }
        if dropped:
            self._emit(
                CacheListener.on_entries_invalidated,
                self._scope,
                dropped,
                CLEAR_ALL_TRIGGER,
            )
        return dropped

    def populate_eager_entries(self, disable_concurrency=False):
        """Populate every eager entry, inside the population events. The first
        failure aborts, exactly like today (see InitializationError)."""
        from winslow.cache.runtime import populate_eager_entries

        with self._population_events():
            populate_eager_entries(self._instances.values(), disable_concurrency)

    def populate_all(self, disable_concurrency=False):
        """Re-run the eager population as a batch action. A failing loader
        reports through on_entry_error; the other entries keep populating."""
        from winslow.cache.runtime import populate_eager_entries

        with self._population_events():
            populate_eager_entries(
                self._instances.values(), disable_concurrency, _populate_quietly
            )

    def _eager_entries(self):
        """The eager entry names per cache, for the population events."""
        return {
            name: fields
            for name, cache in self._instances.items()
            if (fields := tuple(eager_fields(type(cache))))
        }

    @contextmanager
    def _population_events(self):
        """Bracket a population pool with the events, on the calling thread."""
        entries = self._eager_entries()
        self._emit(CacheListener.on_eager_population_started, self._scope, entries)
        try:
            yield entries
        finally:
            self._emit(CacheListener.on_eager_population_finished, self._scope, entries)

    def __getattr__(self, name):
        try:
            return self._instances[name]
        except KeyError:
            raise AttributeError(
                f"Unknown cache '{name}' - known caches: {sorted(self._instances)}"
            ) from None

    def __setattr__(self, name, value):
        raise AttributeError(
            f"The cache container is read-only - cannot assign '{name}'."
        )

    def __repr__(self):
        return f"<CacheContainer {sorted(self._instances)}>"


def _populate_quietly(cache, name):
    # The failure already reaches the listeners and the telemetry from
    # _entry_value, so the batch action logs and continues.
    try:
        getattr(cache, name)
    except Exception:
        cache.logger.error(
            f"Cache '{cache}': populate_all left '{name}' cold.", exc_info=True
        )


class CacheContainerRef:
    """The cache container of one scope, on Task and Graph. A descriptor, not a
    property, so the attributes view drops it (compare _ExecutionFlag)."""

    def __init__(self, stamp_attr, fallback):
        self._stamp_attr = stamp_attr
        self._fallback = fallback

    def __set_name__(self, owner, name):
        self._name = name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        stamped = getattr(obj, self._stamp_attr)
        if stamped is not None:
            return stamped
        return self._fallback()

    def __set__(self, obj, value):
        raise AttributeError(f"'{self._name}' is read-only.")
