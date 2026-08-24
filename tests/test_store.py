"""The store contract: current is the only storage, reads accept a task or
its key, and publish runs outside the lock."""

import logging
import threading
from types import SimpleNamespace

from winslow.bus import SessionBus
from winslow.events import Origin, TaskStatusEvent
from winslow.runner.store import log_task_status
from winslow.store import ReactiveDict
from winslow.task.status import TaskStatus

from harness import build_workflow, run_all
from winslow.constants import Mode


def fresh():
    bus = SessionBus()
    return ReactiveDict(bus), bus


def item(key):
    return SimpleNamespace(identity_key=key)


def test_reads_accept_a_task_or_its_key():
    store, _ = fresh()
    alpha = item("alpha")
    store[alpha] = 1
    assert store[alpha] == 1
    assert store["alpha"] == 1
    assert store.get(alpha) == 1
    assert store.get("alpha") == 1
    assert alpha in store
    assert "alpha" in store
    assert store.get("missing", 42) == 42


def test_iteration_yields_string_keys():
    store, _ = fresh()
    store[item("alpha")] = 1
    store[item("beta")] = 2
    assert sorted(store) == ["alpha", "beta"]
    assert sorted(store.keys()) == ["alpha", "beta"]
    assert dict(store.items()) == {"alpha": 1, "beta": 2}
    assert sorted(store.values()) == [1, 2]
    assert len(store) == 2


def test_redundant_write_publishes_nothing_and_keeps_the_snapshot():
    store, bus = fresh()
    events = []
    bus.subscribe(TaskStatusEvent, events.append)
    store["alpha"] = 1
    snapshot = store.current
    store["alpha"] = 1
    assert store.current is snapshot
    assert [e.key for e in events] == ["alpha"]


def test_each_transition_rebinds_a_fresh_snapshot():
    store, _ = fresh()
    store["alpha"] = 1
    before = store.current
    store["alpha"] = 2
    assert store.current is not before
    assert before == {"alpha": 1}
    assert store.current == {"alpha": 2}


def test_publish_runs_outside_the_lock():
    """A subscriber blocks until another thread completes a write. A dispatch
    under the store lock would deadlock here."""
    store, bus = fresh()
    written = threading.Event()

    def blocking_subscriber(event):
        if event.key != "alpha":
            return
        writer = threading.Thread(target=lambda: store.set("beta", 1))
        writer.start()
        writer.join(timeout=2.0)
        written.set()

    bus.subscribe(TaskStatusEvent, blocking_subscriber)
    store["alpha"] = 1
    assert written.wait(timeout=2.0)
    assert store["beta"] == 1


def test_clear_resets_the_snapshot():
    store, _ = fresh()
    store["alpha"] = 1
    store.clear()
    assert store.current == {}
    assert len(store) == 0


def test_log_subscriber_levels_by_status_and_origin(caplog):
    events = [
        (TaskStatus.INITIALIZED, Origin.RUN, logging.DEBUG),
        (TaskStatus.COMPLETED, Origin.SEED, logging.DEBUG),
        (TaskStatus.COMPLETED, Origin.RUN, logging.INFO),
    ]
    with caplog.at_level(logging.DEBUG, logger="winslow"):
        for status, origin, _ in events:
            log_task_status(TaskStatusEvent(key="alpha", status=status, origin=origin))
    assert [r.levelno for r in caplog.records] == [lvl for _, _, lvl in events]
    assert all("alpha" in r.message for r in caplog.records)


def test_headless_logs_transitions_and_interactive_does_not(e2e_repo, caplog):
    for mode, expected in ((Mode.HEADLESS, True), (Mode.TUI, False)):
        caplog.clear()
        workflow = build_workflow(e2e_repo, "my-workflow", mode)
        with caplog.at_level(logging.INFO, logger="winslow"):
            run_all(workflow)
        logged = any("updated to" in r.message for r in caplog.records)
        assert logged is expected, mode
