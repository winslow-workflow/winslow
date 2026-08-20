import logging
import threading
import time

from argparse import Namespace

import pytest

from winslow.cache import (
    CACHE_LOGGER_NAME,
    CacheContainer,
    MISSING,
    WorkflowCache,
    cache_logger,
    entry,
    populate_eager_entries,
)
from winslow.cache.storage import StorageRecord
from winslow.exceptions import CacheReentrancyError, InitializationError
from winslow.logger import TASK_LOGGER_NAME
from winslow.task.context import LogContext, scoped_log_context


def backdate(cache, name, seconds):
    """Rewrite the record of an entry with an older write time - the ttl tests
    then need no sleep."""
    record = cache._storage.read(name)
    assert record is not MISSING
    cache._storage._records[name] = StorageRecord(
        value=record.value, written_at=time.time() - seconds
    )


class JournalCache(WorkflowCache):
    """The shared fixture shape: every loader appends to a per-instance
    journal, so a test asserts what computed and in which order."""

    class Meta:
        abstract = True

    def __init__(self):
        # The stamp a real workflow provides, so the namespace does not warn.
        super().__init__(Namespace(cache_namespace="wf-00000000"))
        self.loads = []


class Weather(JournalCache):
    @entry(eager=True)
    def cities(self):
        self.loads.append("cities")
        return ("athens", "bergen")

    @entry(eager=True, depends_on="cities")
    def city_index(self):
        # The journal write comes after the read: the cities lock then orders
        # the two entries in `loads`, under any population schedule.
        index = {c: i for i, c in enumerate(self.cities)}
        self.loads.append("city_index")
        return index

    @entry(depends_on="cities")
    def forecast(self):
        self.loads.append("forecast")
        return tuple(c.upper() for c in self.cities)

    @entry
    def climates(self):
        self.loads.append("climates")
        return {"athens": "mild"}


def test_lazy_entry_computes_once():
    cache = Weather()
    assert cache.climates == {"athens": "mild"}
    assert cache.climates == {"athens": "mild"}
    assert cache.loads == ["climates"]


def test_entry_cannot_be_assigned():
    cache = Weather()
    with pytest.raises(AttributeError, match="cache entry"):
        cache.climates = {}


def test_concurrent_first_access_computes_once():
    release = threading.Event()

    class Slow(JournalCache):
        @entry
        def value(self):
            self.loads.append("value")
            release.wait(timeout=5)
            return 42

    cache = Slow()
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(cache.value)) for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    release.set()
    for thread in threads:
        thread.join(timeout=5)

    assert results == [42, 42, 42, 42]
    assert cache.loads == ["value"]


def test_ttl_expiry_recomputes_and_logs(caplog):
    class Timed(JournalCache):
        @entry(ttl=30)
        def temperatures(self):
            self.loads.append("temperatures")
            return object()

    cache = Timed()
    first = cache.temperatures
    assert cache.temperatures is first

    backdate(cache, "temperatures", seconds=60)
    with caplog.at_level(logging.INFO, logger=CACHE_LOGGER_NAME):
        second = cache.temperatures

    assert second is not first
    assert cache.loads == ["temperatures", "temperatures"]
    (record,) = caplog.records
    assert "'temperatures' went stale" in record.getMessage()
    assert "ttl 30s" in record.getMessage()


def test_eager_population_loads_a_dependency_before_its_dependent():
    cache = Weather()
    populate_eager_entries([cache])
    # The lazy entries stay cold: only some workflows need them.
    assert cache.loads == ["cities", "city_index"]
    assert cache.city_index == {"athens": 0, "bergen": 1}


def test_eager_population_covers_every_cache():
    class Other(JournalCache):
        @entry(eager=True)
        def winds(self):
            self.loads.append("winds")
            return (1, 2)

    weather, other = Weather(), Other()
    populate_eager_entries([weather, other], disable_concurrency=True)
    assert weather.loads == ["cities", "city_index"]
    assert other.loads == ["winds"]


def test_eager_failure_is_an_initialization_error_and_retries_cold_fields():
    class Flaky(JournalCache):
        attempts = 0

        @entry(eager=True)
        def good(self):
            self.loads.append("good")
            return 1

        @entry(eager=True)
        def bad(self):
            type(self).attempts += 1
            if type(self).attempts == 1:
                raise RuntimeError("first load fails")
            return 2

    cache = Flaky()
    with pytest.raises(InitializationError, match=r"'flaky\.bad' failed to load"):
        populate_eager_entries([cache], disable_concurrency=True)

    # The loaded field stays in place; only the failed field retries.
    populate_eager_entries([cache], disable_concurrency=True)
    assert cache.loads == ["good"]
    assert Flaky.attempts == 2
    assert cache.bad == 2


def test_invalidate_cascades_to_declared_dependents(caplog):
    cache = Weather()
    populate_eager_entries([cache])
    assert cache.forecast == ("ATHENS", "BERGEN")

    with caplog.at_level(logging.INFO, logger=CACHE_LOGGER_NAME):
        cache.invalidate("cities")

    # Upstream first, then the dependents, the lazy opt-in included.
    (record,) = caplog.records
    assert record.getMessage() == (
        "Cache 'weather': invalidate('cities') dropped 'cities', "
        "'city_index', 'forecast'."
    )

    assert cache.city_index == {"athens": 0, "bergen": 1}
    assert cache.loads.count("cities") == 2
    assert cache.loads.count("city_index") == 2


def test_invalidate_leaves_unrelated_entries_alone():
    cache = Weather()
    populate_eager_entries([cache])
    assert cache.climates == {"athens": "mild"}

    cache.invalidate("city_index")

    assert cache.climates == {"athens": "mild"}
    assert cache.loads.count("climates") == 1
    assert cache.loads.count("cities") == 1


def test_invalidate_cold_or_expired_entry_is_a_noop(caplog):
    class Timed(JournalCache):
        @entry(ttl=30)
        def temperatures(self):
            self.loads.append("temperatures")
            return 1

    cache = Timed()
    with caplog.at_level(logging.INFO, logger=CACHE_LOGGER_NAME):
        cache.invalidate("temperatures")  # cold
        cache.temperatures
        backdate(cache, "temperatures", seconds=60)
        cache.invalidate("temperatures")  # expired
    assert caplog.records == []


def test_invalidate_takes_multiple_names(caplog):
    cache = Weather()
    populate_eager_entries([cache])
    assert cache.forecast == ("ATHENS", "BERGEN")
    assert cache.climates == {"athens": "mild"}

    with caplog.at_level(logging.INFO, logger=CACHE_LOGGER_NAME):
        cache.invalidate("city_index", "climates")

    # One call, one log line: both cascades in one drop, cities untouched.
    (record,) = caplog.records
    assert record.getMessage() == (
        "Cache 'weather': invalidate('city_index', 'climates') dropped "
        "'climates', 'city_index'."
    )
    assert cache.loads.count("cities") == 1


def test_invalidate_needs_at_least_one_name():
    with pytest.raises(TypeError, match="invalidate_all"):
        Weather().invalidate()


def test_invalidate_unknown_name():
    cache = Weather()
    with pytest.raises(AttributeError, match="known entries") as exc_info:
        cache.invalidate("cities", "nope")
    assert "cities" in str(exc_info.value)


def test_invalidate_all(caplog):
    cache = Weather()
    populate_eager_entries([cache])
    cache.forecast

    with caplog.at_level(logging.INFO, logger=CACHE_LOGGER_NAME):
        cache.invalidate_all()

    (record,) = caplog.records
    message = record.getMessage()
    assert "invalidate_all() dropped" in message
    # Upstream before its dependents.
    assert message.index("'cities'") < message.index("'city_index'")

    assert cache.forecast == ("ATHENS", "BERGEN")
    assert cache.loads.count("forecast") == 2


def test_invalidate_does_not_deadlock_with_a_nested_computation():
    started, release = threading.Event(), threading.Event()

    class Nested(JournalCache):
        @entry(eager=True)
        def upstream(self):
            return 1

        @entry(eager=True, depends_on="upstream")
        def downstream(self):
            started.set()
            release.wait(timeout=5)
            # The nested read takes the upstream lock while this computation
            # holds the downstream lock.
            return self.upstream + 1

    cache = Nested()
    results = {}

    reader = threading.Thread(
        target=lambda: results.setdefault("value", cache.downstream)
    )
    reader.start()
    assert started.wait(timeout=5)

    invalidator = threading.Thread(target=lambda: cache.invalidate("upstream"))
    invalidator.start()
    # Give the invalidator time to reach the downstream lock, then let the
    # nested computation finish. A nested drop would deadlock here.
    time.sleep(0.05)
    release.set()
    reader.join(timeout=5)
    invalidator.join(timeout=5)

    assert not reader.is_alive() and not invalidator.is_alive()
    assert results["value"] == 2
    assert cache.downstream == 2


def test_an_undeclared_read_cycle_raises_instead_of_deadlocking():
    """Validation covers only the declared graph. A loader that reads back
    into a field this thread is computing must fail loudly, not hang forever."""

    class Cyclic(JournalCache):
        @entry
        def a(self):
            return self.b

        @entry  # no depends_on: the cycle is invisible to validation
        def b(self):
            return self.a

    with pytest.raises(CacheReentrancyError, match="undeclared read cycle"):
        Cyclic().a


def test_invalidate_inside_the_loader_of_an_affected_entry_raises():
    """The thread holds the locks of its own read chain, so a drop that
    reaches the chain must fail loudly, not hang on the field lock."""

    class SelfDrop(JournalCache):
        @entry
        def value(self):
            self.invalidate("value")
            return 1

    with pytest.raises(CacheReentrancyError, match="computing on the same thread"):
        SelfDrop().value


def test_invalidate_that_cascades_into_the_loader_raises():
    class DropUpstream(JournalCache):
        @entry
        def upstream(self):
            return 1

        @entry(depends_on="upstream")
        def dependent(self):
            # The cascade of the upstream reaches 'dependent' itself.
            self.invalidate("upstream")
            return 2

    with pytest.raises(CacheReentrancyError, match="reaches 'dependent'"):
        DropUpstream().dependent


def test_a_nested_lazy_failure_emits_one_unscoped_error(monkeypatch):
    """The one-emission doctrine: only the outermost read frame reports a
    loader error, a nested frame raises through its caller."""
    import winslow.telemetry

    calls = []
    monkeypatch.setattr(
        winslow.telemetry, "emit_unscoped_error", lambda exc, **kw: calls.append(exc)
    )

    class NestedBoom(JournalCache):
        @entry
        def outer(self):
            return self.inner

        @entry
        def inner(self):
            raise RuntimeError("inner boom")

    with pytest.raises(RuntimeError, match="inner boom"):
        NestedBoom().outer
    assert len(calls) == 1


def test_a_self_read_through_a_helper_raises():
    class SelfRead(JournalCache):
        def helper(self):
            return self.value

        @entry
        def value(self):
            return self.helper()

    with pytest.raises(CacheReentrancyError, match="'value' reads 'value'"):
        SelfRead().value


def test_a_cache_built_outside_a_workflow_warns_about_the_namespace(caplog):
    class Bare(WorkflowCache):
        @entry
        def value(self):
            return 1

    with caplog.at_level(logging.WARNING, logger="winslow"):
        Bare(Namespace())
    assert any("_unscoped" in record.getMessage() for record in caplog.records)


def test_storage_seam_receives_every_operation():
    operations = []

    class RecordingStorage:
        def __init__(self, cache_name, namespace):
            self._records = {}

        def read(self, key):
            operations.append(("read", key))
            return self._records.get(key, MISSING)

        def write(self, key, record):
            operations.append(("write", key))
            self._records[key] = record
            return record

        def delete(self, key):
            operations.append(("delete", key))
            self._records.pop(key, None)

    class Stored(JournalCache):
        storage_class = RecordingStorage

        @entry
        def value(self):
            return 7

    cache = Stored()
    assert cache.value == 7
    cache.invalidate("value")
    assert ("write", "value") in operations
    assert ("delete", "value") in operations


def test_container_rejects_unknown_names_and_assignment():
    container = CacheContainer({"weather": Weather()})
    assert container.weather.cities == ("athens", "bergen")

    with pytest.raises(AttributeError, match="known caches: \\['weather'\\]"):
        container.nope
    with pytest.raises(AttributeError, match="read-only"):
        container.weather = object()


def test_cache_logger_resolves_from_the_ambient_context():
    # The property is the loader-facing form of cache_logger(): resolved per
    # access, so one instance routes differently inside and outside a task.
    cache = Weather()
    outside = cache.logger
    assert isinstance(outside, logging.Logger)
    assert outside.name == CACHE_LOGGER_NAME
    assert cache_logger().name == CACHE_LOGGER_NAME

    context = LogContext(
        session_id="s-1",
        workflow_name="wf",
        workflow_instance="wf",
        task_name="t",
        task_instance="t",
        batch_uuid="b-1",
        task_key="nonce-1:task-abc12345",
    )
    with scoped_log_context(context):
        inside = cache.logger
    assert isinstance(inside, logging.LoggerAdapter)
    assert inside.logger.name == TASK_LOGGER_NAME
    assert inside.extra == {"task_id": "nonce-1:task-abc12345"}
