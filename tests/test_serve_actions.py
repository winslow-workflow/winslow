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

from harness import build_workflow, by_name

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


def test_a_request_that_raises_answers_an_error_frame(e2e_repo, monkeypatch):
    from winslow.model import TaskInfo

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
    assert "serves no workflows" in frames_until(ws, "error")["reason"]
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
