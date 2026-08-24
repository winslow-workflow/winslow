"""The serve slice-two contract: subscribe answers with the session snapshot,
the bridge streams the bus events with sequence numbers, log lines coalesce
per task per tick, a resubscribe is the sequence-gap recovery, and a client
behind a full frame window is dropped and disconnected."""

import asyncio
import collections
import json

from starlette.testclient import TestClient

from winslow.constants import Mode
from winslow.events import LogLineEvent
from winslow.serve import Credentials, create_app
from winslow.serve.bridge import EventBridge, Subscription
from winslow.session import Session, SessionRegistry
from winslow.task.status import TaskStatus as S

from harness import build_workflow, by_name, run_batch

TOKEN = "test-token"


def live_session(e2e_repo, mode=Mode.TUI):
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    return workflow, session


def connect(registry, qsize=10_000):
    credentials = Credentials(token=TOKEN, require_credential=True)
    app = create_app(registry, credentials, hello_timeout=1.0, qsize=qsize)
    client = TestClient(app)
    ws = client.websocket_connect("/ws").__enter__()
    ws.send_json({"type": "hello", "version": 1, "token": TOKEN})
    assert ws.receive_json()["type"] == "hello_ok"
    assert ws.receive_json()["type"] == "snapshot"
    return client, ws


def frames_until(ws, kind, limit=500):
    seen = []
    for _ in range(limit):
        frame = ws.receive_json()
        seen.append(frame)
        if frame["type"] == kind:
            return seen
    raise AssertionError(f"no {kind!r} frame within {limit} frames: {seen[-5:]}")


def test_subscribe_answers_with_the_session_snapshot(e2e_repo):
    workflow, session = live_session(e2e_repo)
    registry = SessionRegistry()
    registry.register(session)
    _, ws = connect(registry)

    ws.send_json({"type": "subscribe", "session_id": session.session_id})
    snapshot = ws.receive_json()
    assert snapshot["type"] == "snapshot"
    assert snapshot["session_id"] == session.session_id
    assert snapshot["seq"] == 0
    assert snapshot["tasks"] == {
        key: status.name for key, status in workflow.store.current.items()
    }
    assert snapshot["batches"] == []
    ws.close()


def test_an_unknown_session_answers_an_error_and_stays_open(e2e_repo):
    workflow, session = live_session(e2e_repo)
    registry = SessionRegistry()
    registry.register(session)
    _, ws = connect(registry)

    ws.send_json({"type": "subscribe", "session_id": "gone"})
    error = ws.receive_json()
    assert error["type"] == "error"
    assert "does not resolve to a live session" in error["reason"]
    ws.send_json({"type": "subscribe", "session_id": session.session_id})
    assert ws.receive_json()["type"] == "snapshot"
    ws.close()


def test_a_batch_streams_its_events_with_increasing_sequence(e2e_repo):
    workflow, session = live_session(e2e_repo)
    registry = SessionRegistry()
    registry.register(session)
    _, ws = connect(registry)
    ws.send_json({"type": "subscribe", "session_id": session.session_id})
    assert ws.receive_json()["type"] == "snapshot"

    run_batch(workflow)

    frames = frames_until(ws, "batch_completed")
    kinds = collections.Counter(frame["type"] for frame in frames)
    assert kinds["batch_created"] == 1
    assert kinds["task_status"] > 0
    assert kinds["execution_status"] > 0
    sequences = [frame["seq"] for frame in frames]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)
    assert all(frame["session_id"] == session.session_id for frame in frames)
    completed = frames[-1]
    assert completed["batch"]["status"] == "FINISHED"
    # The roster holds the admitted tasks: eligibility filtered the rest.
    roster = set(completed["batch"]["tasks"])
    assert roster and roster < {t.identity_key for t in workflow.tasks}
    ws.close()


def test_log_lines_coalesce_per_task_per_tick(e2e_repo):
    workflow, session = live_session(e2e_repo)
    registry = SessionRegistry()
    registry.register(session)
    _, ws = connect(registry)
    ws.send_json({"type": "subscribe", "session_id": session.session_id})
    assert ws.receive_json()["type"] == "snapshot"

    for n in range(3):
        workflow.bus.publish(
            LogLineEvent(task_key="alpha-1", batch_uuid="b-1", line=f"line {n}")
        )
    frame = ws.receive_json()
    assert frame["type"] == "log_batch"
    assert frame["task_key"] == "alpha-1"
    assert frame["lines"] == ["line 0", "line 1", "line 2"]
    ws.close()


def test_a_resubscribe_resets_the_stream_with_a_fresh_snapshot(e2e_repo):
    workflow, session = live_session(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    registry = SessionRegistry()
    registry.register(session)
    _, ws = connect(registry)
    ws.send_json({"type": "subscribe", "session_id": session.session_id})
    first = ws.receive_json()

    run_batch(workflow)
    # Drain the live stream first, so the resubscribe happens after the tick
    # fanned the batch out and the sequence moved.
    frames_until(ws, "batch_completed")

    ws.send_json({"type": "subscribe", "session_id": session.session_id})
    fresh = frames_until(ws, "snapshot")[-1]
    assert fresh["seq"] > first["seq"]
    assert fresh["tasks"][alpha.identity_key] == workflow.store[alpha].name
    ws.close()


def test_a_full_window_of_drops_marks_the_client_too_slow(e2e_repo):
    workflow, session = live_session(e2e_repo)
    bridge = EventBridge(session, qsize=4)
    subscription = Subscription(wake=asyncio.Event(), maxlen=4)
    bridge.subscribe(subscription)

    for n in range(12):
        bridge._fan_out({"type": "task_status", "key": f"k{n}", "status": "RUNNING"})

    assert len(subscription.deque) == 4
    assert subscription.dropped == 8
    assert subscription.behind_a_full_window
    # The frames that survive are the newest, so a reader sees the gap.
    kept = [json.loads(payload)["seq"] for payload in subscription.deque]
    assert kept == [9, 10, 11, 12]


def test_unsubscribe_stops_the_stream(e2e_repo):
    workflow, session = live_session(e2e_repo)
    registry = SessionRegistry()
    registry.register(session)
    _, ws = connect(registry)
    ws.send_json({"type": "subscribe", "session_id": session.session_id})
    assert ws.receive_json()["type"] == "snapshot"

    ws.send_json({"type": "unsubscribe", "session_id": session.session_id})
    assert ws.receive_json()["type"] == "unsubscribed"
    run_batch(workflow)
    assert workflow.store[by_name(workflow)["Alpha"]] is S.COMPLETED

    ws.send_json({"type": "subscribe", "session_id": session.session_id})
    frame = ws.receive_json()
    # The snapshot only: no event of the unsubscribed batch leaked into the queue.
    assert frame["type"] == "snapshot"
    ws.close()


def test_an_unknown_message_type_answers_an_error(e2e_repo):
    workflow, session = live_session(e2e_repo)
    registry = SessionRegistry()
    registry.register(session)
    _, ws = connect(registry)
    ws.send_json({"type": "shout"})
    assert "unknown message type" in ws.receive_json()["reason"]
    ws.close()
