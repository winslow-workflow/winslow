"""The session bus contract: subscription order, isolation of a raising
subscriber, the close sweep, the origin filter of the persistence subscriber,
and the session-ended dispatch after the durable writes."""

import pytest

from winslow.bus import SessionBus
from winslow.constants import Mode
from winslow.events import (
    ExecutionStatusEvent,
    Origin,
    SessionEndedEvent,
    TaskStatusEvent,
)
from winslow.exceptions import RegistrationError
from winslow.session import Session
from winslow.state import SessionPersistenceAdapter
from winslow.task.status import TaskStatus

from harness import build_workflow, by_name, ready, run_batch


def test_dispatch_order_is_subscription_order():
    bus = SessionBus()
    order = []
    for index in range(5):
        bus.subscribe(SessionEndedEvent, lambda event, i=index: order.append(i))

    bus.publish(SessionEndedEvent(session_id="s"))

    assert order == [0, 1, 2, 3, 4]


def test_a_raising_subscriber_does_not_stop_the_dispatch():
    bus = SessionBus()
    seen = []

    def boom(event):
        raise RuntimeError("boom")

    bus.subscribe(SessionEndedEvent, boom)
    bus.subscribe(SessionEndedEvent, seen.append)

    bus.publish(SessionEndedEvent(session_id="s"))

    assert seen == [SessionEndedEvent(session_id="s")]


def test_close_is_idempotent_and_final():
    bus = SessionBus()
    seen = []
    bus.subscribe(SessionEndedEvent, seen.append)

    bus.close()
    bus.close()
    bus.publish(SessionEndedEvent(session_id="s"))

    assert seen == []
    with pytest.raises(RegistrationError):
        bus.subscribe(SessionEndedEvent, seen.append)


def test_the_persistence_subscriber_acts_only_on_run_writes(state_store):
    adapter = SessionPersistenceAdapter(state_store, "sess-origin")
    try:
        for origin in (Origin.REPLAY, Origin.SEED):
            adapter.on_task_status(
                TaskStatusEvent(key="k", status=TaskStatus.COMPLETED, origin=origin)
            )
        adapter.flush()
        assert state_store.load_status_snapshots("sess-origin") == {}

        adapter.on_task_status(
            TaskStatusEvent(key="k", status=TaskStatus.COMPLETED)
        )
        adapter.flush()
        assert set(state_store.load_status_snapshots("sess-origin")) == {"k"}
    finally:
        adapter.close()


def test_a_subscription_during_a_batch_receives_that_batch(e2e_repo):
    # The listener copy of the old runner froze the audience at batch start.
    # One bus subscription covers the running batch from the moment it lands.
    workflow = ready(build_workflow(e2e_repo, "my-workflow", Mode.TUI))
    alpha = by_name(workflow)["Alpha"]
    first_seen, late = [], []

    def second(event):
        late.append(event.batch_uuid)

    def first(event):
        if not first_seen:
            workflow.bus.subscribe(ExecutionStatusEvent, second)
        first_seen.append(event.batch_uuid)

    workflow.bus.subscribe(ExecutionStatusEvent, first)
    run_batch(workflow, [alpha])

    assert late
    assert set(late) == set(first_seen)


def test_session_ended_reaches_a_subscriber_after_the_archive(
    e2e_repo, state_store, mode
):
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    workflow.init_state(state_store, origin="test")
    ended = []

    def record(event):
        # The open-manifest list at dispatch time proves that the durable
        # writes landed before the event.
        ended.append((event.session_id, state_store.list_open_manifests()))

    workflow.bus.subscribe(SessionEndedEvent, record)
    session.end()

    assert ended == [(session.session_id, [])]
