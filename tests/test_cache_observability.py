"""The observability layers of cache-ui-spec.md: peek and inspect, the
CacheListener events, the container actions, and the history capture of the
cache reads of a task phase."""

import logging
import threading
import time

from argparse import Namespace

import pytest

from winslow.cache import (
    CacheContainer,
    CacheListener,
    EntryState,
    ErrorOrigin,
    JsonFileStorage,
    MemoryStorage,
    MISSING,
    StorageRecord,
    WorkflowCache,
    compose,
    entry,
    recording_cache_reads,
)
from winslow.constants import Mode
from winslow.exceptions import MisconfigurationError
from winslow.logger import run_logger_name
from winslow.task.context import LogContext, scoped_log_context
from winslow.runner.execution import ExecutionPhase
from winslow.bus import SessionBus
from winslow.events import TaskStatusEvent
from winslow.store import ReactiveDict

from harness import build_workflow, run_all


def backdate(cache, name, seconds):
    """Rewrite the record of an entry with an older write time - the ttl tests
    then need no sleep."""
    record = cache._storage.read(name)
    assert record is not MISSING
    cache._storage._records[name] = StorageRecord(
        value=record.value, written_at=time.time() - seconds
    )


class JournalCache(WorkflowCache):
    class Meta:
        abstract = True

    def __init__(self):
        super().__init__(Namespace(cache_namespace="wf-00000000"))
        self.loads = []


class Weather(JournalCache):
    @entry(eager=True)
    def cities(self):
        self.loads.append("cities")
        return ("athens", "bergen")

    @entry(depends_on="cities", ttl=30)
    def forecast(self):
        self.loads.append("forecast")
        return tuple(c.upper() for c in self.cities)


class _Events(CacheListener):
    def __init__(self):
        self.events = []

    def on_entry_computed(self, info, previous_state):
        self.events.append(("computed", info.entry_name, previous_state))

    def on_entries_invalidated(self, scope, dropped, trigger):
        self.events.append(("invalidated", scope, dropped, trigger))

    def on_eager_population_started(self, scope, entries):
        self.events.append(("population_started", scope, entries))

    def on_eager_population_finished(self, scope, entries):
        self.events.append(("population_finished", scope, entries))

    def on_entry_error(self, scope, cache_name, entry_name, error):
        self.events.append(("entry_error", cache_name, entry_name, error.message))


def subscribed(*caches):
    container = CacheContainer({cache.get_name(): cache for cache in caches})
    listener = _Events()
    container.add_listener(listener)
    return container, listener


# --- peek and inspect ---------------------------------------------------


def test_peek_never_computes():
    cache = Weather()
    assert cache.peek("cities") is MISSING
    assert cache.loads == []

    assert cache.cities == ("athens", "bergen")
    assert cache.peek("cities").value == ("athens", "bergen")
    assert cache.loads == ["cities"]


def test_peek_rejects_an_unknown_entry():
    with pytest.raises(AttributeError, match="known entries"):
        Weather().peek("nope")


def test_peek_reports_computing_while_a_loader_runs():
    loading = threading.Event()
    release = threading.Event()

    class Slow(JournalCache):
        @entry
        def levels(self):
            loading.set()
            release.wait(timeout=5)
            return (1, 2)

    cache = Slow()
    reader = threading.Thread(target=lambda: cache.levels)
    reader.start()
    try:
        assert loading.wait(timeout=5)
        assert cache.peek("levels") is EntryState.COMPUTING
        (info,) = cache.inspect()
        assert info.state is EntryState.COMPUTING
        assert info.written_at is None
    finally:
        release.set()
        reader.join(timeout=5)
    assert cache.peek("levels").value == (1, 2)


def test_composed_peek_does_not_promote(tmp_path):
    class TmpJson(JsonFileStorage):
        base_directory = tmp_path

    class Tiered(JournalCache):
        name = "tiered"
        storage_class = compose(MemoryStorage, TmpJson)

        @entry
        def values(self):
            return {"a": 1}

    warm = Tiered()
    assert warm.values == {"a": 1}

    # A fresh instance holds the value in the file tier only. peek must not
    # copy it into the memory tier; read promotes it.
    cold = Tiered()
    memory = cold._storage._tiers[0]
    assert cold.peek("values").value == {"a": 1}
    assert memory.read("values") is MISSING
    assert cold.values == {"a": 1}
    assert memory.read("values") is not MISSING


def test_storage_labels(tmp_path):
    class TmpJson(JsonFileStorage):
        base_directory = tmp_path

    class Tiered(JournalCache):
        name = "labeled"
        storage_class = compose(MemoryStorage, TmpJson)

        @entry
        def values(self):
            return 1

    assert Tiered().describe_storage() == "MemoryStorage over TmpJson"
    assert Weather().describe_storage() == "MemoryStorage"


def test_inspect_derives_the_state_at_peek_time():
    cache = Weather()
    container, _ = subscribed(cache)

    by_name = {info.entry_name: info for info in container.inspect()}
    assert by_name["cities"].state is EntryState.COLD
    assert by_name["cities"].written_at is None
    assert by_name["cities"].eager is True
    assert by_name["forecast"].depends_on == ("cities",)
    assert by_name["forecast"].ttl == 30
    assert by_name["forecast"].scope == "workflow"
    assert by_name["forecast"].storage == "MemoryStorage"

    assert cache.forecast == ("ATHENS", "BERGEN")
    by_name = {info.entry_name: info for info in cache.inspect()}
    assert by_name["forecast"].state is EntryState.WARM
    assert by_name["forecast"].written_at is not None

    backdate(cache, "forecast", seconds=31)
    by_name = {info.entry_name: info for info in cache.inspect()}
    assert by_name["forecast"].state is EntryState.STALE
    assert by_name["cities"].state is EntryState.WARM


def test_container_rejects_a_cache_that_shadows_its_api():
    class Inspect(JournalCache):
        name = "inspect"

        @entry
        def values(self):
            return 1

    with pytest.raises(MisconfigurationError, match="clash"):
        CacheContainer({"inspect": Inspect()})


# --- events ---------------------------------------------------------------


def test_entry_computed_reports_the_previous_state():
    cache = Weather()
    _, listener = subscribed(cache)

    assert cache.forecast
    assert listener.events == [
        ("computed", "cities", "cold"),
        ("computed", "forecast", "cold"),
    ]

    listener.events.clear()
    backdate(cache, "forecast", seconds=31)
    assert cache.forecast
    assert listener.events == [("computed", "forecast", "stale")]


def test_invalidation_emits_once_with_the_live_cascade():
    cache = Weather()
    _, listener = subscribed(cache)
    assert cache.forecast
    listener.events.clear()

    cache.invalidate("cities")
    assert listener.events == [
        (
            "invalidated",
            "workflow",
            {"weather": ("cities", "forecast")},
            "invalidate('cities')",
        )
    ]

    # Every entry is cold now: a second invalidation drops no live value and
    # emits nothing.
    listener.events.clear()
    cache.invalidate_all()
    assert listener.events == []


def test_loader_error_reports_the_outermost_entry():
    class Broken(JournalCache):
        @entry
        def upstream(self):
            raise ValueError("boom")

        @entry(depends_on="upstream")
        def dependent(self):
            return self.upstream

    cache = Broken()
    _, listener = subscribed(cache)

    with pytest.raises(ValueError):
        cache.dependent
    assert listener.events == [("entry_error", "broken", "dependent", "boom")]

    # The golden source carries the error: the outermost entry is ERRORED
    # with a loader origin, the upstream stays plain cold.
    infos = {i.entry_name: i for i in cache.inspect()}
    assert infos["dependent"].state is EntryState.ERRORED
    assert infos["dependent"].error.origin is ErrorOrigin.LOAD
    assert infos["dependent"].error.tier is None
    assert "ValueError: boom" in infos["dependent"].error.traceback
    assert infos["upstream"].state is EntryState.COLD
    assert cache.peek("dependent") is MISSING


def test_clear_all_emits_one_event_across_caches():
    class Stations(JournalCache):
        @entry
        def names(self):
            return ("north",)

    weather, stations = Weather(), Stations()
    container, listener = subscribed(weather, stations)
    assert weather.cities and stations.names
    listener.events.clear()

    dropped = container.clear_all()
    assert dropped == {"weather": ("cities",), "stations": ("names",)}
    assert listener.events == [("invalidated", "workflow", dropped, "clear_all")]


def test_population_events_bracket_the_pool():
    cache = Weather()
    container, listener = subscribed(cache)

    container.populate_eager_entries(disable_concurrency=True)
    assert listener.events[0] == (
        "population_started",
        "workflow",
        {"weather": ("cities",)},
    )
    assert listener.events[1] == ("computed", "cities", "cold")
    assert listener.events[2] == (
        "population_finished",
        "workflow",
        {"weather": ("cities",)},
    )


def test_populate_all_continues_past_a_failing_loader():
    class Flaky(JournalCache):
        @entry(eager=True)
        def bad(self):
            raise ValueError("boom")

        @entry(eager=True)
        def good(self):
            return 1

    cache = Flaky()
    container, listener = subscribed(cache)

    container.populate_all(disable_concurrency=True)
    events = {event[0] for event in listener.events}
    assert "population_started" in events and "population_finished" in events
    assert ("entry_error", "flaky", "bad", "boom") in listener.events
    assert cache.peek("good").value == 1
    assert cache.peek("bad") is MISSING


def test_remove_listener_stops_the_delivery():
    cache = Weather()
    container, listener = subscribed(cache)
    container.remove_listener(listener)
    container.remove_listener(listener)  # idempotent

    assert cache.cities
    assert listener.events == []


def test_bus_unsubscribe_stops_the_delivery():
    statuses = []

    def record(event):
        statuses.append((event.key, event.status))

    bus = SessionBus()
    store = ReactiveDict(bus)
    bus.subscribe(TaskStatusEvent, record)
    store["a"] = 1
    bus.unsubscribe(TaskStatusEvent, record)
    bus.unsubscribe(TaskStatusEvent, record)  # idempotent
    store["a"] = 2
    assert statuses == [("a", 1)]


# --- history capture -------------------------------------------------------


def _task_stub(container):
    return Namespace(_workflow_cache_container=container, _global_cache_container=None)


def test_recording_sweep_renders_the_last_read_and_drops_the_records():
    cache = Weather()
    container, _ = subscribed(cache)
    task = _task_stub(container)

    with recording_cache_reads(task) as recorder:
        proxy = task._workflow_cache_container
        assert proxy.weather.forecast == ("ATHENS", "BERGEN")
        assert proxy.weather.forecast == ("ATHENS", "BERGEN")

    # The stamps are restored on exit.
    assert task._workflow_cache_container is container

    snapshots = {s.entry_name: s for s in recorder.sweep()}
    assert set(snapshots) == {"forecast"}
    snap = snapshots["forecast"]
    assert (snap.scope, snap.cache_name) == ("workflow", "weather")
    assert "ATHENS" in snap.rendered
    assert snap.summary is None
    assert snap.written_at is not None
    assert recorder.sweep() == ()


def test_snapshot_cap_truncates_with_a_summary():
    class Big(JournalCache):
        snapshot_size_bytes = 16

        @entry
        def payload(self):
            return {"key_" + str(i): "x" * 50 for i in range(10)}

    class Silent(JournalCache):
        snapshot_size_bytes = 0

        @entry
        def payload(self):
            return [1, 2, 3]

    big, silent = Big(), Silent()
    container, _ = subscribed(big, silent)
    task = _task_stub(container)

    with recording_cache_reads(task) as recorder:
        assert task._workflow_cache_container.big.payload
        assert task._workflow_cache_container.silent.payload

    snapshots = {s.cache_name: s for s in recorder.sweep()}
    assert len(snapshots["big"].rendered.encode()) <= 16
    assert snapshots["big"].summary.startswith("dict, len 10, keys:")
    assert snapshots["silent"].rendered == ""
    assert snapshots["silent"].summary == "list, len 3"


def test_a_container_longer_than_the_cap_skips_the_render():
    class Wide(JournalCache):
        snapshot_size_bytes = 64

        @entry
        def payload(self):
            return list(range(100))

    container, _ = subscribed(Wide())
    task = _task_stub(container)

    with recording_cache_reads(task) as recorder:
        assert task._workflow_cache_container.wide.payload

    # The length alone proves the overrun, so no head is rendered.
    (snapshot,) = recorder.sweep()
    assert snapshot.rendered == ""
    assert snapshot.summary == "list, len 100"


def test_a_raising_repr_degrades_one_snapshot_not_the_sweep():
    class Hostile:
        def __repr__(self):
            raise RuntimeError("boom")

    class Mixed(JournalCache):
        @entry
        def bad(self):
            return Hostile()

        @entry
        def good(self):
            return "fine"

    container, _ = subscribed(Mixed())
    task = _task_stub(container)

    with recording_cache_reads(task) as recorder:
        assert task._workflow_cache_container.mixed.bad is not None
        assert task._workflow_cache_container.mixed.good == "fine"

    snapshots = {s.entry_name: s for s in recorder.sweep()}
    assert snapshots["bad"].summary == "<unrepresentable: RuntimeError: boom>"
    assert snapshots["good"].rendered == "'fine'"


def test_nested_recording_wraps_the_original_container():
    cache = Weather()
    container, _ = subscribed(cache)
    task = _task_stub(container)

    with recording_cache_reads(task) as outer:
        with recording_cache_reads(task) as inner:
            assert task._workflow_cache_container._wrapped is container
            assert task._workflow_cache_container.weather.cities
        # The inner phase recorded its read; the outer scope saw nothing.
        assert len(inner.sweep()) == 1
        assert outer.sweep() == ()
    assert task._workflow_cache_container is container


def test_run_records_the_cache_reads_per_phase(e2e_repo):
    workflow = build_workflow(e2e_repo, "my-cache", Mode.TUI)
    run_all(workflow)

    stores = list(workflow.runner.execution_record_store_map.values())
    assert len(stores) == 1
    reads = {}
    for record in stores[0].records:
        for phase, snapshots in record.cache_snapshots.items():
            for snap in snapshots:
                reads.setdefault((phase, snap.cache_name, snap.entry_name), snap)

    index_read = reads[(ExecutionPhase.RUN, "weather", "city_index")]
    assert index_read.scope == "workflow"
    assert "athens" in index_read.rendered
    forecast_read = reads[(ExecutionPhase.RUN, "weather", "forecast")]
    assert "ATHENS" in forecast_read.rendered
    # Plain strings only: history outlives the session and its caches.
    assert isinstance(forecast_read.rendered, str)


def test_container_serves_its_caches_in_name_order():
    class Alpha(JournalCache):
        @entry
        def value(self):
            return 1

    class Zulu(JournalCache):
        @entry
        def value(self):
            return 1

    container, _ = subscribed(Zulu(), Alpha())
    assert [cache.get_name() for cache in container.caches()] == ["alpha", "zulu"]


def test_cache_logs_route_to_the_session_logger(caplog):
    """A session-scoped emission outside a task, for example a UI action,
    lands on the session logger that the log pane and the sinks consume."""
    cache = Weather()
    context = LogContext(
        session_id="s-route",
        workflow_name="weather",
        workflow_instance="weather",
        task_name=None,
        task_instance=None,
        batch_uuid=None,
    )
    with (
        caplog.at_level(logging.INFO, logger=run_logger_name("s-route")),
        scoped_log_context(context),
    ):
        assert cache.cities
        cache.invalidate("cities")
    assert [record.name for record in caplog.records] == [run_logger_name("s-route")]
    assert "dropped 'cities'" in caplog.records[0].message


def test_snapshot_encodings_follow_the_display_style():
    """TREE stores JSON for the history tree; a non-JSON value and a custom
    formatter store text; RAW stays the pretty text of today."""
    import json

    from winslow.cache import DisplayStyle, SnapshotEncoding

    class Styled(JournalCache):
        @entry(display_style=DisplayStyle.TREE)
        def shaped(self):
            return {"a": [1, 2]}

        @entry(display_style=DisplayStyle.TREE)
        def unshaped(self):
            return {"obj": object()}

        @entry(display_style=lambda value: f"total={sum(value)}")
        def formatted(self):
            return (1, 2, 3)

        @entry
        def plain(self):
            return {"b": 1}

    container, _ = subscribed(Styled())
    task = _task_stub(container)

    with recording_cache_reads(task) as recorder:
        cache = task._workflow_cache_container.styled
        assert cache.shaped and cache.unshaped and cache.formatted and cache.plain

    snapshots = {s.entry_name: s for s in recorder.sweep()}
    assert snapshots["shaped"].encoding is SnapshotEncoding.JSON
    assert json.loads(snapshots["shaped"].rendered) == {"a": [1, 2]}
    assert snapshots["unshaped"].encoding is SnapshotEncoding.TEXT
    assert snapshots["unshaped"].rendered.startswith("{'obj'")
    assert snapshots["formatted"].encoding is SnapshotEncoding.TEXT
    assert snapshots["formatted"].rendered == "total=6"
    assert snapshots["plain"].encoding is SnapshotEncoding.TEXT


def test_a_raising_listener_is_logged_and_skipped(caplog):
    """An observer must not break the observed operation: the read succeeds,
    the failure logs with its traceback, the other listeners still hear."""

    class Bomb(CacheListener):
        def on_entry_computed(self, info, previous_state):
            raise RuntimeError("listener boom")

    cache = Weather()
    container, listener = subscribed(cache)
    container.add_listener(Bomb())

    with caplog.at_level(logging.ERROR):
        assert cache.cities == ("athens", "bergen")

    assert ("computed", "cities", "cold") in listener.events
    assert any(
        "failed on on_entry_computed" in record.message for record in caplog.records
    )


def test_a_raising_telemetry_hook_does_not_skip_the_quarantine(monkeypatch):
    """The quarantine precedes the report: a failing reporting step must not
    leave the entry unmarked."""
    import winslow.cache.base as base_module

    def boom(cache, name, exc):
        raise RuntimeError("telemetry down")

    monkeypatch.setattr(base_module, "emit_lazy_error", boom)

    class Failing(JournalCache):
        @entry
        def value(self):
            raise ValueError("loader boom")

    cache = Failing()
    _, listener = subscribed(cache)

    with pytest.raises(RuntimeError, match="telemetry down"):
        cache.value

    (info,) = cache.inspect()
    assert info.state is EntryState.ERRORED
    assert info.error.origin is ErrorOrigin.LOAD
    assert ("entry_error", "failing", "value", "loader boom") in listener.events
