"""The session port, slice four: the wire transport, black-box against a
served process. RemoteAppClient and RemoteSessionClient answer the same
DTOs the local adapter hands through, the subscriptions relay the protocol
frames as model events, and a dropped connection reconnects, resubscribes
and heals its subscribers from the recovery snapshot."""

import threading
import time

import pytest
import uvicorn

from winslow.actions import BatchAck, EndSession, LoadCacheEntries, RunTasks, StopBatch
from winslow.client.local import LocalSessionClient
from winslow.client.websocket import RemoteAppClient, RemoteSessionClient, normalize_url
from winslow.constants import Mode
from winslow.events import (
    BatchCompletedEvent,
    BatchCreatedEvent,
    LogLineEvent,
    SessionEndedEvent,
    TaskStatusEvent,
)
from winslow.exceptions import MisconfigurationError, RequestError
from winslow.model import (
    BatchInfo,
    CacheCard,
    CacheUpdatedEvent,
    ConnectionEvent,
    SessionLogEvent,
    SourceNode,
    TaskLogEvent,
    TaskOutcome,
)
from winslow.orchestrator import Action, Orchestrator, OrchestratorConfig
from winslow.serve import Credentials, create_app
from winslow.session import Session, SessionRegistry
from winslow.task.status import TaskStatus

from harness import build_workflow, by_name, wait_for_status

from test_serve_actions import serve_orchestrator

TOKEN = "test-token"


class ServedProcess:
    """One uvicorn process in a thread, the way `winslow serve` runs it,
    with a stop and a same-port restart for the reconnect tests."""

    def __init__(self, registry, orchestrator=None, state_store=None):
        self.app = create_app(
            registry,
            Credentials(token=TOKEN, require_credential=True),
            hello_timeout=2.0,
            orchestrator=orchestrator,
            state_store=state_store,
        )
        self.server = None
        self.thread = None
        self.port = 0

    def start(self):
        config = uvicorn.Config(
            self.app, host="127.0.0.1", port=self.port, log_level="warning"
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise AssertionError("the serve process never started")
            time.sleep(0.01)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        return self

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}"


def registered(e2e_repo, name="my-workflow", mode=Mode.TUI):
    workflow = build_workflow(e2e_repo, name, mode)
    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    registry = SessionRegistry()
    registry.register(session)
    return workflow, session, registry


@pytest.fixture
def served(e2e_repo):
    """A served session and a connected wire client, torn down afterwards."""
    workflow, session, registry = registered(e2e_repo)
    process = ServedProcess(registry).start()
    client = RemoteAppClient(process.url, token=TOKEN).connect()
    try:
        yield workflow, session, client
    finally:
        client.close()
        process.stop()


def wait_for(predicate, message, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(message)


def run_to_completion(workflow, client, task):
    ack = client.submit(RunTasks(keys=(task.identity_key,)))
    assert ack.accepted, ack.reason
    workflow.runner.get_batch(ack.batch_uuid).wait()
    return ack


# --- the reads: the wire answers the DTOs of the local adapter -----------------


def test_the_wire_reads_match_the_local_adapter(served):
    workflow, session, client = served
    remote = client.session(session.session_id)
    local = LocalSessionClient(session)
    alpha = by_name(workflow)["Alpha"]
    run_to_completion(workflow, remote, alpha)

    assert remote.snapshot() == local.snapshot()
    assert remote.roster() == local.roster()
    assert remote.history() == local.history()
    assert remote.batch_options() == local.batch_options()
    assert remote.session_params() == local.session_params()
    batch_uuid = remote.history()[0].uuid
    key = alpha.identity_key
    assert remote.record_detail(batch_uuid, key) == local.record_detail(
        batch_uuid, key
    )
    assert remote.log_tail(batch_uuid, key) == local.log_tail(batch_uuid, key)
    assert remote.apply_filter("alpha") == local.apply_filter("alpha")


def test_task_detail_decodes_the_full_capture(served):
    workflow, session, client = served
    remote = client.session(session.session_id)
    alpha = by_name(workflow)["Alpha"]
    info = remote.task_detail(alpha.identity_key)
    assert info.key == alpha.identity_key
    assert isinstance(info.source, SourceNode)
    assert info.source.source
    titles = [section[0] for section in info.attributes]
    assert "Class Attributes" in titles
    assert info.docs is not None


def test_history_rows_carry_task_outcome_instances(served):
    workflow, session, client = served
    remote = client.session(session.session_id)
    run_to_completion(workflow, remote, by_name(workflow)["Alpha"])
    (row,) = remote.history()
    (outcome,) = row.tasks.values()
    assert isinstance(outcome, TaskOutcome)
    assert outcome.status == "COMPLETED"


def test_a_run_streams_the_model_events(served):
    workflow, session, client = served
    remote = client.session(session.session_id)
    alpha = by_name(workflow)["Alpha"]

    statuses, created, completed, lines = [], [], [], []
    done = threading.Event()
    remote.subscribe(TaskStatusEvent, statuses.append)
    remote.subscribe(BatchCreatedEvent, created.append)
    remote.subscribe(LogLineEvent, lines.append)
    remote.subscribe(BatchCompletedEvent, lambda e: (completed.append(e), done.set()))

    ack = remote.submit(RunTasks(keys=(alpha.identity_key,)))
    assert isinstance(ack, BatchAck) and ack.accepted
    assert done.wait(10), "no batch_completed event over the wire"

    assert isinstance(created[0].info, BatchInfo)
    assert created[0].info.uuid == ack.batch_uuid
    assert completed[0].info.status == "FINISHED"
    assert {e.key for e in statuses} == {alpha.identity_key}
    assert all(isinstance(e.status, TaskStatus) for e in statuses)
    assert statuses[-1].status is TaskStatus.COMPLETED
    assert all(isinstance(e, LogLineEvent) for e in lines)


def test_unsubscribe_stops_a_handler(served):
    workflow, session, client = served
    remote = client.session(session.session_id)
    statuses = []
    remote.subscribe(TaskStatusEvent, statuses.append)
    remote.unsubscribe(TaskStatusEvent, statuses.append)
    run_to_completion(workflow, remote, by_name(workflow)["Alpha"])
    time.sleep(0.3)
    assert statuses == []


def test_the_session_log_lane_streams(served):
    workflow, session, client = served
    remote = client.session(session.session_id)
    lines = []
    remote.subscribe(SessionLogEvent, lines.append)
    time.sleep(0.3)
    workflow.logger.warning("hello over the wire")
    wait_for(
        lambda: any("hello over the wire" in e.line for e in lines),
        "the session log line never arrived",
    )


def test_task_log_backlog_then_live_lines(served, monkeypatch):
    workflow, session, client = served
    remote = client.session(session.session_id)
    alpha = by_name(workflow)["Alpha"]
    original = type(alpha).run

    def run(self):
        self.logger.warning("alpha task-log hello")
        original(self)

    monkeypatch.setattr(type(alpha), "run", run)

    lines = []
    backlog = remote.subscribe_task_log(alpha.identity_key, lines.append)
    assert backlog == ()
    run_to_completion(workflow, remote, alpha)
    wait_for(
        lambda: any("alpha task-log hello" in e.line for e in lines),
        "the live task log line never arrived",
    )
    assert all(isinstance(e, TaskLogEvent) for e in lines)

    remote.unsubscribe_task_log(alpha.identity_key, lines.append)
    seen = len(lines)
    run_to_completion(workflow, remote, alpha)
    time.sleep(0.3)
    assert len(lines) == seen


def test_subscribe_task_log_refuses_an_unknown_task(served):
    workflow, session, client = served
    remote = client.session(session.session_id)
    with pytest.raises(RequestError):
        remote.subscribe_task_log("no-such-task", lambda e: None)


def test_cache_reads_actions_and_the_update_lane(e2e_repo):
    workflow, session, registry = registered(e2e_repo, "my-cache")
    process = ServedProcess(registry).start()
    client = RemoteAppClient(process.url, token=TOKEN).connect()
    try:
        remote = client.session(session.session_id)
        updates = []
        remote.subscribe(CacheUpdatedEvent, updates.append)

        cards = remote.caches()
        assert all(isinstance(card, CacheCard) for card in cards)
        (weather,) = [card for card in cards if card.name == "weather"]
        assert {entry.name for entry in weather.entries} == {
            "cities",
            "city_index",
            "forecast",
        }

        assert remote.cache_value("weather", "forecast").state == "cold"
        ack = remote.submit(LoadCacheEntries(entries=(("weather", "forecast"),)))
        assert ack.accepted, ack.reason
        wait_for(
            lambda: remote.cache_value("weather", "forecast").state == "warm",
            "the load never warmed the entry",
        )
        assert "ATHENS" in remote.cache_value("weather", "forecast").rendered
        wait_for(
            lambda: any(e.cache_name == "weather" for e in updates),
            "no cache_updated event over the wire",
        )
    finally:
        client.close()
        process.stop()


# --- the actions ----------------------------------------------------------------


def test_a_refused_action_answers_a_refused_ack(served):
    workflow, session, client = served
    remote = client.session(session.session_id)
    ack = remote.submit(StopBatch(batch_uuid="no-such-batch"))
    assert ack.accepted is False
    assert "no-such-batch" in ack.reason


def test_an_unknown_action_class_answers_a_refused_ack(served):
    workflow, session, client = served
    remote = client.session(session.session_id)
    ack = remote.submit(object())
    assert ack.accepted is False
    assert "names no action" in ack.reason


def test_end_session_emits_the_event_and_keeps_the_ended_reads(served):
    workflow, session, client = served
    remote = client.session(session.session_id)
    ended = threading.Event()
    remote.subscribe(SessionEndedEvent, lambda e: ended.set())
    time.sleep(0.3)

    ack = remote.submit(EndSession())
    assert ack.accepted
    assert ended.wait(10), "no session_ended event over the wire"
    wait_for(lambda: remote.snapshot().status == "ENDED", "the session never ENDED")
    # A live-guarded read refuses with direction once the session ended.
    with pytest.raises(RequestError, match="has ended"):
        remote.roster()


# --- the app scope over the wire --------------------------------------------------


def test_create_session_over_the_wire(e2e_repo, state_store):
    orchestrator = serve_orchestrator(e2e_repo)
    registry = SessionRegistry()
    process = ServedProcess(
        registry, orchestrator=orchestrator, state_store=state_store
    ).start()
    client = RemoteAppClient(process.url, token=TOKEN).connect()
    try:
        descriptors = client.descriptors()
        assert "my-workflow" in {d.workflow for d in descriptors.workflows}

        row = client.create_session("my-workflow")
        assert row.status == "ACTIVE"
        assert row.session_id in registry

        alpha_key = next(
            key for key in client.session(row.session_id).snapshot().tasks
        )
        assert alpha_key

        # A dead process leaves an open manifest; restore rebuilds the session.
        registry.remove(row.session_id)
        (manifest,) = [
            m for m in client.manifests() if m.session_id == row.session_id
        ]
        restored = client.restore_session(manifest.session_id)
        assert restored.session_id == row.session_id
        assert row.session_id in registry
    finally:
        client.close()
        process.stop()


def test_create_session_error_carries_the_server_traceback(e2e_repo, state_store):
    orchestrator = serve_orchestrator(e2e_repo)
    registry = SessionRegistry()
    process = ServedProcess(
        registry, orchestrator=orchestrator, state_store=state_store
    ).start()
    client = RemoteAppClient(process.url, token=TOKEN).connect()
    try:
        with pytest.raises(RequestError) as excinfo:
            client.create_session("no-such-workflow")
        assert "Traceback" in excinfo.value.detail
    finally:
        client.close()
        process.stop()


# --- the connection: handshake, reconnect, gap recovery ---------------------------


def test_a_bad_token_refuses_the_connect(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    process = ServedProcess(registry).start()
    try:
        with pytest.raises(MisconfigurationError, match="bad bearer token"):
            RemoteAppClient(process.url, token="wrong").connect()
    finally:
        process.stop()


def test_an_unreachable_server_refuses_the_connect():
    with pytest.raises(MisconfigurationError, match="cannot connect"):
        RemoteAppClient("ws://127.0.0.1:1", open_timeout=1.0).connect()


def test_normalize_url_appends_the_ws_route():
    assert normalize_url("ws://host:8866") == "ws://host:8866/ws"
    assert normalize_url("ws://host:8866/") == "ws://host:8866/ws"
    assert normalize_url("wss://host:8866/custom") == "wss://host:8866/custom"
    with pytest.raises(MisconfigurationError, match="not a websocket URL"):
        normalize_url("http://host:8866")


def test_the_connect_subcommand_parses_the_url():
    config, _ = Orchestrator.get_base_parser().parse_known_args(
        ["connect", "ws://somehost:8866"], namespace=OrchestratorConfig()
    )
    assert config.action is Action.CONNECT
    assert config.url == "ws://somehost:8866"


def test_a_reconnect_resubscribes_and_heals_the_subscribers(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    process = ServedProcess(registry).start()
    client = RemoteAppClient(process.url, token=TOKEN).connect()
    try:
        remote = client.session(session.session_id)
        statuses = []
        connection_events = []
        done = threading.Event()
        remote.subscribe(TaskStatusEvent, statuses.append)
        remote.subscribe(BatchCompletedEvent, lambda e: done.set())
        client.subscribe_connection(connection_events.append)
        time.sleep(0.3)

        process.stop()
        wait_for(
            lambda: _read_refused(remote),
            "a read during the outage never refused",
        )
        wait_for(
            lambda: ConnectionEvent(connected=False) in connection_events,
            "the drop never emitted a ConnectionEvent",
        )

        # The same port again: the client reconnects on its own and the
        # recovery snapshot re-emits the task statuses.
        process.start()
        wait_for(lambda: statuses, "no healing events after the reconnect")
        assert {e.key for e in statuses} == set(remote.snapshot().tasks)
        wait_for(
            lambda: connection_events[-1] == ConnectionEvent(connected=True),
            "the recovery never emitted a ConnectionEvent",
        )

        # The lane is live again: a run streams to completion.
        alpha = by_name(workflow)["Alpha"]
        ack = remote.submit(RunTasks(keys=(alpha.identity_key,)))
        assert ack.accepted, ack.reason
        assert done.wait(10), "no batch_completed after the reconnect"
        wait_for_status(workflow, alpha, TaskStatus.COMPLETED)
    finally:
        client.close()
        process.stop()


def _read_refused(remote):
    try:
        remote.snapshot()
        return False
    except (ConnectionError, ValueError, TimeoutError):
        return True


class RecordingWire:
    """The send side of a lane under test: the gap recovery is a client
    rule, so the test drives on_frame directly and reads what the lane
    sends back."""

    def __init__(self):
        self.sent = []

    def send(self, frame):
        self.sent.append(frame)

    def next_id(self):
        return "c-test"

    def subscribe_session(self, lane):
        self.send(
            {
                "type": "subscribe",
                "session_id": lane.session_id,
                "request_id": self.next_id(),
            }
        )

    def clear_subscribe_pending(self, lane):
        pass


def _snapshot_frame(seq, tasks, status="ACTIVE", batches=()):
    return {
        "type": "snapshot",
        "seq": seq,
        "session_id": "s-1",
        "workflow": "w",
        "status": status,
        "tasks": tasks,
        "session_log_backlog": [],
        "batches": list(batches),
        "cache_names": [],
    }


def _batch_payload(uuid, completed_at=None):
    return {
        "uuid": uuid,
        "action": "RUN",
        "status": "FINISHED" if completed_at else "RUNNING",
        "task_count": 1,
        "tasks": {"k": "K"},
        "options": None,
        "created_at": 1.0,
        "started_at": 1.0,
        "completed_at": completed_at,
        "error": None,
    }


def _status_frame(seq, key, status):
    return {
        "type": "task_status",
        "seq": seq,
        "session_id": "s-1",
        "key": key,
        "status": status,
        "origin": "run",
    }


def test_a_sequence_gap_resubscribes_and_heals_from_the_snapshot():
    wire = RecordingWire()
    lane = RemoteSessionClient(wire, "s-1")
    events = []
    lane.subscribe(TaskStatusEvent, events.append)
    assert wire.sent[-1]["type"] == "subscribe"

    lane.on_frame(_snapshot_frame(5, {"k": "READY_TO_PROCESS"}))
    lane.on_frame(_status_frame(6, "k", "RUNNING"))
    assert [e.status for e in events] == [TaskStatus.RUNNING]

    # The gap: seq 7 and 8 are lost. The lane resubscribes and drops the
    # stale frames until the recovery snapshot arrives.
    lane.on_frame(_status_frame(9, "k", "RUN_FINISHED"))
    assert wire.sent[-1]["type"] == "subscribe"
    assert wire.sent[-1]["session_id"] == "s-1"
    lane.on_frame(_status_frame(10, "k", "CHECKING_COMPLETION"))
    assert [e.status for e in events] == [TaskStatus.RUNNING]

    lane.on_frame(_snapshot_frame(12, {"k": "COMPLETED"}))
    assert [e.status for e in events] == [TaskStatus.RUNNING, TaskStatus.COMPLETED]

    # The stream continues from the snapshot's sequence.
    lane.on_frame(_status_frame(13, "k", "FAILED"))
    assert events[-1].status is TaskStatus.FAILED


def test_a_recovery_snapshot_re_emits_its_batches():
    """The events of the gap are gone; the created and completed events of
    the snapshot batches rebuild the history of a live subscriber."""
    wire = RecordingWire()
    lane = RemoteSessionClient(wire, "s-1")
    created, completed = [], []
    lane.subscribe(BatchCreatedEvent, created.append)
    lane.subscribe(BatchCompletedEvent, completed.append)

    # The first snapshot heals nothing: no batch events re-emit.
    lane.on_frame(_snapshot_frame(5, {}, batches=[_batch_payload("b-1")]))
    assert created == []

    lane.on_frame(_status_frame(9, "k", "RUNNING"))  # the gap
    lane.on_frame(
        _snapshot_frame(
            12,
            {},
            batches=[
                _batch_payload("b-1", completed_at=2.0),
                _batch_payload("b-2"),
            ],
        )
    )
    assert [info.uuid for info in (e.info for e in created)] == ["b-1", "b-2"]
    assert [e.info.uuid for e in completed] == ["b-1"]
    assert created[0].info.tasks == {"k": "K"}


def test_a_recovery_snapshot_of_an_ended_session_emits_the_end():
    wire = RecordingWire()
    lane = RemoteSessionClient(wire, "s-1")
    ended = []
    lane.subscribe(SessionEndedEvent, ended.append)
    lane.on_frame(_snapshot_frame(1, {}))
    lane.on_frame(_status_frame(5, "k", "RUNNING"))  # the gap
    lane.on_frame(_snapshot_frame(8, {}, status="ENDED"))
    assert ended == [SessionEndedEvent(session_id="s-1")]


def test_a_refused_subscribe_emits_the_end_instead_of_a_dead_lane():
    wire = RecordingWire()
    lane = RemoteSessionClient(wire, "s-1")
    ended = []
    lane.subscribe(SessionEndedEvent, ended.append)
    lane.on_subscribe_refused("session id 's-1' does not resolve")
    assert ended == [SessionEndedEvent(session_id="s-1")]


def test_a_reconnect_onto_a_lost_session_emits_the_end(e2e_repo):
    """The serve process restarts with an empty registry: the resubscribe
    is refused, and the lane surfaces the end instead of waiting for a
    snapshot forever."""
    workflow, session, registry = registered(e2e_repo)
    process = ServedProcess(registry).start()
    client = RemoteAppClient(process.url, token=TOKEN).connect()
    fresh = None
    try:
        remote = client.session(session.session_id)
        ended = threading.Event()
        remote.subscribe(SessionEndedEvent, lambda e: ended.set())
        time.sleep(0.3)

        process.stop()
        fresh = ServedProcess(SessionRegistry())
        fresh.port = process.port
        fresh.start()

        assert ended.wait(15), "the refused resubscribe never surfaced the end"
    finally:
        client.close()
        if fresh is not None:
            fresh.stop()
