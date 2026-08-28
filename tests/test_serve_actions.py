"""The serve slice-three contract: actions over the wire answer with typed
acks under their request id, the read requests serve descriptors, history,
log tails and task detail, and create_session builds a live session the
whole protocol then drives end to end."""

from starlette.testclient import TestClient

from winslow.constants import Mode
from winslow.orchestrator import Orchestrator, OrchestratorConfig
from winslow.serve import Credentials, create_app
from winslow.serve.app import PROTOCOL_VERSION
from winslow.session import Session, SessionRegistry
from winslow.task.status import TaskStatus as S

from harness import build_workflow, by_name, wait_for_status

TOKEN = "test-token"


def serve_orchestrator(directory, *unknown_args):
    """unknown_args mirrors the trailing CLI tokens a real `winslow serve`
    invocation would leave unclaimed - the per-workflow prefill values (see
    Orchestrator.collect_workflow_args)."""
    config, unknown = Orchestrator.get_base_parser().parse_known_args(
        ["serve", *unknown_args], namespace=OrchestratorConfig()
    )
    orchestrator = Orchestrator(config, directory=directory, unknown_args=unknown)
    orchestrator.workflow_registry.collect_classes(directory)
    return orchestrator


def connect(registry, orchestrator=None, state_store=None):
    app = create_app(
        registry,
        Credentials(token=TOKEN, require_credential=True),
        hello_timeout=1.0,
        orchestrator=orchestrator,
        state_store=state_store,
    )
    ws = TestClient(app).websocket_connect("/ws").__enter__()
    ws.send_json({"type": "hello", "version": PROTOCOL_VERSION, "token": TOKEN})
    assert ws.receive_json()["type"] == "hello_ok"
    assert ws.receive_json()["type"] == "snapshot"
    return ws


def frames_until(ws, kind, limit=500):
    for _ in range(limit):
        frame = ws.receive_json()
        if frame["type"] == kind:
            return frame
    raise AssertionError(f"no {kind!r} frame within {limit} frames")


def registered(e2e_repo, mode=Mode.TUI):
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    registry = SessionRegistry()
    registry.register(session)
    return workflow, session, registry


def test_a_run_action_answers_an_ack_and_streams_the_batch(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    ws = connect(registry)
    ws.send_json({"type": "subscribe", "session_id": session.session_id})
    assert ws.receive_json()["type"] == "snapshot"

    ws.send_json(
        {
            "type": "action",
            "request_id": "r-1",
            "session_id": session.session_id,
            "action": "run_tasks",
            "fields": {"keys": [alpha.identity_key]},
        }
    )
    ack = frames_until(ws, "ack")
    assert ack["request_id"] == "r-1"
    assert ack["accepted"] is True
    completed = frames_until(ws, "batch_completed")
    assert completed["batch"]["uuid"] == ack["batch_uuid"]
    assert workflow.store[alpha] is S.COMPLETED
    ws.close()


def test_a_refused_action_carries_the_reason(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    ws = connect(registry)
    ws.send_json(
        {
            "type": "action",
            "request_id": "r-2",
            "session_id": session.session_id,
            "action": "stop_batch",
            "fields": {"batch_uuid": "no-such-batch"},
        }
    )
    ack = frames_until(ws, "ack")
    assert ack["accepted"] is False
    assert "no-such-batch" in ack["reason"]
    ws.close()


def test_an_unknown_action_name_answers_an_error(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    ws = connect(registry)
    ws.send_json(
        {
            "type": "action",
            "request_id": "r-3",
            "session_id": session.session_id,
            "action": "explode",
        }
    )
    error = frames_until(ws, "error")
    assert error["request_id"] == "r-3"
    assert "names no action" in error["reason"]
    ws.close()


def test_bad_action_fields_answer_an_error(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    ws = connect(registry)
    ws.send_json(
        {
            "type": "action",
            "request_id": "r-4",
            "session_id": session.session_id,
            "action": "run_tasks",
            "fields": {"nope": 1},
        }
    )
    assert "bad fields for run_tasks" in frames_until(ws, "error")["reason"]
    ws.close()


def test_submit_guarded_turns_a_raise_into_a_refused_ack(e2e_repo, monkeypatch):
    from winslow.actions import ActionHandler, EndSession

    workflow, session, registry = registered(e2e_repo)

    def explode(self, action):
        raise RuntimeError("boom")

    monkeypatch.setitem(ActionHandler._methods, EndSession, explode)
    ack = session.actions.submit_guarded(EndSession())
    assert ack.accepted is False
    assert "the session log has the traceback" in ack.reason


def test_history_serves_the_batches_with_their_outcomes(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    ws = connect(registry)
    ws.send_json(
        {
            "type": "action",
            "request_id": "r-5",
            "session_id": session.session_id,
            "action": "run_tasks",
            "fields": {"keys": [alpha.identity_key]},
        }
    )
    ack = frames_until(ws, "ack")
    wait_for_status(workflow, alpha, S.COMPLETED)
    ws.send_json(
        {
            "type": "request",
            "request_id": "r-6",
            "kind": "history",
            "session_id": session.session_id,
        }
    )
    result = frames_until(ws, "result")
    (row,) = result["batches"]
    assert row["uuid"] == ack["batch_uuid"]
    assert row["tasks"][alpha.identity_key] == "COMPLETED"
    ws.close()


def test_log_tail_serves_the_captured_lines(e2e_repo, monkeypatch):
    workflow, session, registry = registered(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    original = type(alpha).run

    def run(self):
        self.logger.warning("alpha says hello")
        original(self)

    monkeypatch.setattr(type(alpha), "run", run)
    ws = connect(registry)
    ws.send_json(
        {
            "type": "action",
            "request_id": "r-7",
            "session_id": session.session_id,
            "action": "run_tasks",
            "fields": {"keys": [alpha.identity_key]},
        }
    )
    ack = frames_until(ws, "ack")
    wait_for_status(workflow, alpha, S.COMPLETED)
    ws.send_json(
        {
            "type": "request",
            "request_id": "r-8",
            "kind": "log_tail",
            "session_id": session.session_id,
            "batch_uuid": ack["batch_uuid"],
            "task_key": alpha.identity_key,
        }
    )
    result = frames_until(ws, "result")
    assert any("alpha says hello" in line for line in result["lines"])

    ws.send_json(
        {
            "type": "request",
            "request_id": "r-9",
            "kind": "log_tail",
            "session_id": session.session_id,
            "batch_uuid": "gone",
            "task_key": alpha.identity_key,
        }
    )
    assert "keeps no records" in frames_until(ws, "error")["reason"]
    ws.close()


def test_a_request_that_raises_answers_an_error_frame(e2e_repo, monkeypatch):
    from winslow.task.info import TaskInfo

    workflow, session, registry = registered(e2e_repo)
    alpha = by_name(workflow)["Alpha"]

    def explode(*args, **kwargs):
        raise RuntimeError("capture exploded")

    monkeypatch.setattr(TaskInfo, "from_task", classmethod(explode))
    ws = connect(registry)
    ws.send_json(
        {
            "type": "request",
            "request_id": "r-10",
            "kind": "task_detail",
            "session_id": session.session_id,
            "task_key": alpha.identity_key,
        }
    )
    error = frames_until(ws, "error")
    assert error["request_id"] == "r-10"
    assert "server log" in error["reason"]
    ws.close()


def test_task_detail_serves_the_full_capture(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    ws = connect(registry)
    ws.send_json(
        {
            "type": "request",
            "request_id": "r-10",
            "kind": "task_detail",
            "session_id": session.session_id,
            "task_key": alpha.identity_key,
        }
    )
    result = frames_until(ws, "result")
    assert result["info"]["key"] == alpha.identity_key
    ws.close()


def test_descriptors_serve_the_start_form_options(e2e_repo):
    orchestrator = serve_orchestrator(e2e_repo)
    ws = connect(SessionRegistry(), orchestrator=orchestrator)
    ws.send_json({"type": "request", "request_id": "r-11", "kind": "descriptors"})
    result = frames_until(ws, "result")
    names = [row["workflow"] for row in result["workflows"]]
    assert "my-workflow" in names
    ws.close()


def test_create_session_builds_and_registers_a_live_session(e2e_repo, state_store):
    orchestrator = serve_orchestrator(e2e_repo)
    registry = SessionRegistry()
    ws = connect(registry, orchestrator=orchestrator, state_store=state_store)

    ws.send_json(
        {
            "type": "request",
            "request_id": "r-12",
            "kind": "create_session",
            "workflow": "my-workflow",
        }
    )
    result = frames_until(ws, "result")
    session_id = result["session_id"]
    assert session_id in registry
    assert result["status"] == "ACTIVE"

    # The created session serves the whole protocol: subscribe and run.
    ws.send_json({"type": "subscribe", "session_id": session_id})
    snapshot = frames_until(ws, "snapshot")
    key = next(k for k in snapshot["tasks"] if k.startswith("alpha"))
    ws.send_json(
        {
            "type": "action",
            "request_id": "r-13",
            "session_id": session_id,
            "action": "run_tasks",
            "fields": {"keys": [key]},
        }
    )
    assert frames_until(ws, "ack")["accepted"] is True
    assert frames_until(ws, "batch_completed")["batch"]["status"] == "FINISHED"
    ws.close()


def test_create_session_refuses_an_unknown_workflow(e2e_repo, state_store):
    orchestrator = serve_orchestrator(e2e_repo)
    ws = connect(SessionRegistry(), orchestrator=orchestrator, state_store=state_store)
    ws.send_json(
        {
            "type": "request",
            "request_id": "r-14",
            "kind": "create_session",
            "workflow": "nope",
        }
    )
    assert "names no collected workflow" in frames_until(ws, "error")["reason"]
    ws.close()


def test_requests_without_an_orchestrator_answer_an_error(e2e_repo):
    ws = connect(SessionRegistry())
    ws.send_json({"type": "request", "request_id": "r-15", "kind": "descriptors"})
    assert "serves no workflows" in frames_until(ws, "error")["reason"]
    ws.send_json(
        {
            "type": "request",
            "request_id": "r-16",
            "kind": "create_session",
            "workflow": "whatever",
        }
    )
    assert "creates no sessions" in frames_until(ws, "error")["reason"]
    ws.close()


def test_descriptors_carry_the_overrides_and_form_fields(e2e_repo):
    orchestrator = serve_orchestrator(e2e_repo)
    ws = connect(SessionRegistry(), orchestrator=orchestrator)
    ws.send_json({"type": "request", "request_id": "r-17", "kind": "descriptors"})
    result = frames_until(ws, "result")

    identified = next(
        row for row in result["workflows"] if row["workflow"] == "my-identified"
    )
    (client,) = [o for o in identified["options"] if o["name"] == "client"]
    assert client["required"] is True
    assert client["identifier"] is True
    assert client["depends_on"] == []

    override_names = [o["name"] for o in result["overrides"]]
    assert "dry_run" in override_names
    ws.close()


def test_create_session_refuses_a_missing_required_value(e2e_repo, state_store):
    orchestrator = serve_orchestrator(e2e_repo)
    registry = SessionRegistry()
    ws = connect(registry, orchestrator=orchestrator, state_store=state_store)
    ws.send_json(
        {
            "type": "request",
            "request_id": "r-18",
            "kind": "create_session",
            "workflow": "my-identified",
        }
    )
    error = frames_until(ws, "error")
    assert "requires client" in error["reason"]
    assert "descriptors" in error["reason"]
    # Refused before any initialization: nothing registered.
    assert len(registry) == 0
    ws.close()


def test_create_session_refuses_an_unknown_value_name(e2e_repo, state_store):
    orchestrator = serve_orchestrator(e2e_repo)
    ws = connect(SessionRegistry(), orchestrator=orchestrator, state_store=state_store)
    ws.send_json(
        {
            "type": "request",
            "request_id": "r-19",
            "kind": "create_session",
            "workflow": "my-workflow",
            "values": {"clientt": "acme"},
        }
    )
    error = frames_until(ws, "error")
    assert "'clientt' names no option" in error["reason"]
    ws.close()


def test_create_session_refuses_a_value_outside_the_choices(e2e_repo, state_store):
    orchestrator = serve_orchestrator(e2e_repo)
    ws = connect(SessionRegistry(), orchestrator=orchestrator, state_store=state_store)
    ws.send_json(
        {
            "type": "request",
            "request_id": "r-20",
            "kind": "create_session",
            "workflow": "my-workflow",
            "overrides": {"mode": "warp"},
        }
    )
    error = frames_until(ws, "error")
    assert "'warp' is not a choice of mode" in error["reason"]
    ws.close()
