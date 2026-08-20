class CacheListener:
    """Observer of the changes in the caches of one container. Subscribe with
    CacheContainer.add_listener. Unsubscribe with remove_listener at session
    end: the global container outlives every session and would otherwise pin
    a dead adapter.

    A callback runs on the thread that changed the cache. on_entry_computed
    and on_entry_error run under the field lock of the entry; the population
    events can run under the process-level population lock, so a callback
    must not call back into the cache runtime. A callback must not block, and
    must not take a lock that another thread holds while it waits on this one
    (see SessionBus). An invalidation cascade emits once, after all drops,
    outside the field locks. A raising callback is logged and skipped (see
    ListenerMixin._emit).

    A callback carries names and projections, never a value and never a cache
    object, so a wire consumer reads the same contract. Each callback does
    nothing by default, so a listener overrides only the events it needs."""

    def on_entry_computed(self, info, previous_state):
        """A loader ran and stored a value. previous_state is the EntryState
        before the run: COLD, STALE or ERRORED. A cleanly invalidated entry
        computes as a cold one: the drop deleted its record."""

    def on_entries_invalidated(self, scope, dropped, trigger):
        """One cascade dropped live values: {cache_name: (entry names, ...)}.
        trigger is the action label, for example "invalidate('cities')"."""

    def on_eager_population_started(self, scope, entries):
        """A population pool starts: {cache_name: (eager entry names, ...)}."""

    def on_eager_population_finished(self, scope, entries):
        """The population pool of on_eager_population_started ended."""

    def on_entry_error(self, scope, cache_name, entry_name, error):
        """A loader or a drop failed on the entry. `error` is the
        CacheEntryError; the entry reports ERRORED until a write clears it.
        For a loader, only the outermost entry of a nested read reports."""
