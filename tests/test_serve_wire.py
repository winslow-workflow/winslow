"""Pure payload tests that need no live session or websocket: the DTO
constructors take a cache object directly and return a plain dict, and a
snapshot turns into the events that produced it."""

import time
from argparse import Namespace

from winslow.cache import WorkflowCache, entry
from dataclasses import asdict

from winslow.events import (
    BatchCompletedEvent,
    BatchCreatedEvent,
    Origin,
    SessionEndedEvent,
    TaskStatusEvent,
)
from winslow.model import BatchInfo, CacheInfo, SessionSnapshot
from winslow.task.status import TaskStatus


def cache_card_payload(cache):
    return asdict(CacheInfo.from_cache(cache))


class FlakyCache(WorkflowCache):
    """A ttl'd entry that succeeds once, then fails every recompute - the
    shape of a value that goes stale and cannot refresh."""

    def __init__(self, workflow_config):
        super().__init__(workflow_config)
        self.calls = 0

    @entry(ttl=0.01)
    def value(self):
        self.calls += 1
        if self.calls == 1:
            return "first value"
        raise RuntimeError("boom on recompute")


def flaky_cache():
    return FlakyCache(Namespace(cache_namespace="test"))


def test_cache_card_omits_the_preview_of_an_errored_entry():
    cache = flaky_cache()
    cache.value  # populates with "first value"
    time.sleep(0.02)  # past the ttl
    try:
        cache.value  # recompute fails, leaves the old value quarantined
    except RuntimeError:
        pass

    card = cache_card_payload(cache)
    (info,) = [i for i in card["info"] if i["entry_name"] == "value"]
    assert info["state"] == "errored"
    assert "value" not in card["values"]


def test_cache_card_previews_a_warm_entry():
    cache = flaky_cache()
    cache.value

    card = cache_card_payload(cache)
    assert card["values"]["value"] == "first value"


def _batch(uuid, completed_at=None):
    return BatchInfo(
        uuid=uuid,
        action="RUN",
        status="COMPLETED" if completed_at else "RUNNING",
        task_count=1,
        tasks={"k": "K"},
        options=None,
        created_at=1.0,
        started_at=1.0,
        completed_at=completed_at,
        error=None,
    )


def test_a_snapshot_replays_as_the_events_that_produced_it():
    """Batches first, each completed one right after its creation, then the
    task statuses, then the end (see SessionSnapshot.as_events)."""
    done, live = _batch("b-1", completed_at=2.0), _batch("b-2")
    snapshot = SessionSnapshot(
        session_id="s-1",
        workflow="wf",
        status="ENDED",
        tasks={"a": "COMPLETED", "b": "RUNNING"},
        session_log_backlog=(),
        batches=(done, live),
    )
    assert snapshot.as_events() == (
        BatchCreatedEvent(info=done),
        BatchCompletedEvent(info=done),
        BatchCreatedEvent(info=live),
        TaskStatusEvent(key="a", status=TaskStatus.COMPLETED, origin=Origin.RUN),
        TaskStatusEvent(key="b", status=TaskStatus.RUNNING, origin=Origin.RUN),
        SessionEndedEvent(session_id="s-1"),
    )


def test_a_live_snapshot_replays_no_end():
    snapshot = SessionSnapshot(
        session_id="s-1",
        workflow="wf",
        status="ACTIVE",
        tasks={},
        session_log_backlog=(),
        batches=(),
    )
    assert snapshot.as_events() == ()
