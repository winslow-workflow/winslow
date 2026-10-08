"""The permission check of the port: the decorators on the handlers and the
reads, the two handlers that refine their gate from the session state, the
filtered list reads, the subscribe check of a connection, and the doors that
carry a refusal as data. A hand-built context stands in for the scoped
credentials of the later steps."""

import logging

import pytest
from starlette.testclient import TestClient

from winslow.actions import (
    ActionHandler,
    CheckTasks,
    EndSession,
    RunTasks,
    StopBatch,
)
from winslow.authz import BODY_GUARDED, OPEN
from winslow.client import LocalAppClient, LocalSessionClient
from winslow.client.base import AppClient, SessionClient
from winslow.constants import Mode
from winslow.exceptions import RequestError
from winslow.orchestrator import Orchestrator, OrchestratorConfig
from winslow.permissions import CredentialKind, Permission, PermissionContext, expand
from winslow.logger import ContextStampFilter
from winslow.protocol.frames import (
    ErrorFrame,
    HelloFrame,
    HelloOkFrame,
    SubscribeFrame,
    TaskLogSubscribeFrame,
)
from winslow.serve import create_app
from winslow.session import Session, SessionRegistry
from winslow.task.context import scoped_permissions
from winslow.task.status import TaskStatus as S
from winslow._meta import _tagged

from harness import (
    bare_orchestrator,
    build_workflow,
    by_name,
    fixed_credentials,
    run_all,
    scratch_state_store,
    start_gated_batch,
    wait_for_status,
)
from test_serve_mcp import call_tools, mcp_app

P = Permission


def context(*spelled, user="lucas"):
    return PermissionContext(
        user=user, scopes=expand(spelled), credential=CredentialKind.TICKET
    )


def live(e2e_repo, name="my-workflow"):
    workflow = build_workflow(e2e_repo, name, Mode.TUI)
    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    return workflow, session


def restricted(*spelled):
    return fixed_credentials(context(*spelled))


# --- completeness -----------------------------------------------------------------


def test_every_handler_is_guarded():
    unguarded = [
        m.__name__
        for m in _tagged(ActionHandler, "handles")
        if not hasattr(m, "requires")
    ]
    assert unguarded == []


def test_every_read_is_guarded_filtered_body_guarded_or_open():
    for name in SessionClient.read_meta:
        assert hasattr(getattr(LocalSessionClient, name), "requires"), name
    for name in AppClient.read_meta:
        method = getattr(LocalAppClient, name)
        guarded = hasattr(method, "requires") or hasattr(method, "visible")
        assert guarded or name in BODY_GUARDED or name in OPEN, name
    assert OPEN == {"host_stats"}


# --- the handlers -----------------------------------------------------------------


def test_a_local_caller_passes_everything(e2e_repo):
    workflow, session = live(e2e_repo)
    ack = session.actions.submit(
        CheckTasks(keys=(by_name(workflow)["Alpha"].identity_key,))
    )
    assert ack.accepted


def test_view_reads_and_refuses_every_action(e2e_repo):
    workflow, session = live(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    port = LocalSessionClient(session)
    with scoped_permissions(context("my-workflow:view_task")):
        assert port.snapshot().session_id == session.session_id
        assert len(port.roster()) == len(workflow.tasks)
        for action in (
            RunTasks(keys=(alpha.identity_key,)),
            CheckTasks(keys=(alpha.identity_key,)),
            EndSession(force=False),
        ):
            ack = session.actions.submit(action)
            assert ack.accepted is False
            assert "lucas lacks my-workflow:" in ack.reason
            assert "the scopes cover my-workflow:view_task" in ack.reason


def test_check_submits_a_check_batch_and_no_run_batch(e2e_repo):
    workflow, session = live(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    with scoped_permissions(context("my-workflow:check_task")):
        refused = session.actions.submit(RunTasks(keys=(alpha.identity_key,)))
        assert refused.accepted is False
        assert "lacks my-workflow:run_task" in refused.reason
        ack = session.actions.submit(CheckTasks(keys=(alpha.identity_key,)))
        assert ack.accepted


def test_run_implies_check(e2e_repo):
    workflow, session = live(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    with scoped_permissions(context("my-workflow:run_task")):
        assert session.actions.submit(CheckTasks(keys=(alpha.identity_key,))).accepted
        assert session.actions.submit(RunTasks(keys=(alpha.identity_key,))).accepted
    wait_for_status(workflow, alpha, S.COMPLETED)


def test_a_scope_on_another_name_does_not_reach_this_session(e2e_repo):
    workflow, session = live(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    with scoped_permissions(context("reports:run_task")):
        ack = session.actions.submit(CheckTasks(keys=(alpha.identity_key,)))
        assert ack.accepted is False
        assert "the scopes cover nothing" in ack.reason
        with pytest.raises(RequestError, match="lacks my-workflow:view_task"):
            LocalSessionClient(session).snapshot()


def test_a_stop_needs_the_permission_of_the_batch_kind(e2e_repo):
    workflow = build_workflow(e2e_repo, "my-gates", Mode.TUI)
    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    gate, batch = start_gated_batch(workflow, by_name(workflow))
    try:
        with scoped_permissions(context("my-gates:check_task")):
            ack = session.actions.submit(StopBatch(batch_uuid=batch.uuid))
        assert ack.accepted is False
        assert "a run batch stops on my-gates:run_task alone" in ack.reason
        with scoped_permissions(context("my-gates:run_task")):
            ack = session.actions.submit(StopBatch(batch_uuid=batch.uuid))
        assert ack.accepted
    finally:
        gate.set()
        batch.wait()


def test_check_ends_a_settled_session_and_not_an_unsettled_one(e2e_repo):
    # my-depends: every task passes, so a full run settles the session.
    workflow, session = live(e2e_repo, "my-depends")
    with scoped_permissions(context("my-depends:check_task")):
        ack = session.actions.submit(EndSession(force=False))
    assert ack.accepted is False
    assert "not settled - ending needs my-depends:end_workflow" in ack.reason
    assert str(session.unsettled_count) in ack.reason

    run_all(workflow)
    assert session.unsettled_count == 0
    with scoped_permissions(context("my-depends:check_task")):
        ack = session.actions.submit(EndSession(force=False))
    assert ack.accepted
    assert session.has_ended


def test_end_workflow_ends_an_unsettled_session(e2e_repo):
    workflow, session = live(e2e_repo)
    with scoped_permissions(context("my-workflow:end_workflow")):
        assert session.actions.submit(EndSession(force=False)).accepted


# --- the app reads ------------------------------------------------------------------


def collecting_orchestrator(directory):
    config, unknown = Orchestrator.get_base_parser().parse_known_args(
        ["serve"], namespace=OrchestratorConfig()
    )
    orchestrator = Orchestrator(config, directory=directory, unknown_args=unknown)
    orchestrator.workflow_registry.collect_classes(directory)
    return orchestrator


def test_the_list_reads_filter_to_the_scopes_of_the_caller(e2e_repo, state_store):
    registry = SessionRegistry()
    port = LocalAppClient(
        registry,
        orchestrator=collecting_orchestrator(e2e_repo),
        state_store=state_store,
    )
    info = port.create_session("my-workflow")
    other = port.create_session("my-gates")
    registry.remove(other.session_id)  # a manifest with no live session

    with scoped_permissions(
        context("my-workflow:view_task", "my-gates:start_workflow")
    ):
        assert [row.session_id for row in port.sessions()] == [info.session_id]
        assert [d.workflow for d in port.descriptors().workflows] == ["my-gates"]
        assert [m.session_id for m in port.manifests()] == [other.session_id]
    with scoped_permissions(context("reports:view_task")):
        assert port.sessions() == ()
        assert port.descriptors().workflows == ()
        assert port.manifests() == ()
        assert port.host_stats().hostname


def test_start_and_restore_need_start_workflow(e2e_repo, state_store):
    registry = SessionRegistry()
    port = LocalAppClient(
        registry,
        orchestrator=collecting_orchestrator(e2e_repo),
        state_store=state_store,
    )
    with scoped_permissions(context("my-workflow:view_task")):
        with pytest.raises(RequestError, match="lacks my-workflow:start_workflow"):
            port.create_session("my-workflow")
    with scoped_permissions(context("my-workflow:start_workflow")):
        info = port.create_session("my-workflow")
    registry.remove(info.session_id)
    with scoped_permissions(context("my-workflow:view_task")):
        with pytest.raises(RequestError, match="lacks my-workflow:start_workflow"):
            port.restore_session(info.session_id)
    with scoped_permissions(context("my-workflow:start_workflow")):
        assert port.restore_session(info.session_id).session_id == info.session_id


# --- the doors ----------------------------------------------------------------------


def test_the_websocket_refuses_a_subscribe_without_view(e2e_repo):
    workflow, session = live(e2e_repo)
    registry = SessionRegistry()
    registry.register(session)
    app = create_app(
        registry,
        restricted("reports:view_task"),
        orchestrator=bare_orchestrator(),
        state_store=scratch_state_store(),
    )
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_json({"type": HelloFrame.type, "token": "t"})
        hello_ok = ws.receive_json()
        assert hello_ok["type"] == HelloOkFrame.type
        assert hello_ok["scopes"] == ["reports:view_task"]
        ws.send_json(
            {
                "type": SubscribeFrame.type,
                "session_id": session.session_id,
                "request_id": "r-1",
            }
        )
        error = ws.receive_json()
        assert error["type"] == ErrorFrame.type
        assert "lacks my-workflow:view_task" in error["reason"]


def test_the_mcp_door_carries_the_refusal_as_data(e2e_repo):
    workflow, session = live(e2e_repo)
    registry = SessionRegistry()
    registry.register(session)
    alpha = by_name(workflow)["Alpha"]
    (ack, snapshot) = call_tools(
        mcp_app(registry, restricted("my-workflow:view_task")),
        [
            (
                "run_tasks",
                {"session_id": session.session_id, "keys": [alpha.identity_key]},
            ),
            ("snapshot", {"session_id": session.session_id}),
        ],
        token="t",
    )
    assert ack["accepted"] is False
    assert "lacks my-workflow:run_task" in ack["reason"]
    assert snapshot["session_id"] == session.session_id


# --- the audit lines and the log stamp ---------------------------------------------


def audit_records(caplog):
    return [r for r in caplog.records if r.name == "winslow.audit"]


def test_the_audit_logger_takes_one_line_per_action_and_none_per_read(e2e_repo, caplog):
    workflow, session = live(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    caplog.set_level(logging.INFO, logger="winslow.audit")
    with scoped_permissions(context("my-workflow:check_task", user="lucas")):
        LocalSessionClient(session).snapshot()
        assert audit_records(caplog) == []
        session.actions.submit(CheckTasks(keys=(alpha.identity_key,)))
        session.actions.submit(RunTasks(keys=(alpha.identity_key,)))
    accepted, refused = audit_records(caplog)
    assert accepted.audit == {
        "action": "check_tasks",
        "scope": "my-workflow",
        "session": session.session_id,
        "keys": 1,
        "result": "accepted",
        "reason": None,
    }
    assert "user=lucas credential=ticket action=check_tasks" in accepted.getMessage()
    assert refused.audit["result"] == "refused"
    assert "lacks my-workflow:run_task" in refused.audit["reason"]


def test_a_refused_start_leaves_an_audit_line(e2e_repo, state_store, caplog):
    port = LocalAppClient(
        SessionRegistry(),
        orchestrator=collecting_orchestrator(e2e_repo),
        state_store=state_store,
    )
    caplog.set_level(logging.INFO, logger="winslow.audit")
    with scoped_permissions(context("my-workflow:view_task")):
        with pytest.raises(RequestError):
            port.create_session("my-workflow")
    (refused,) = audit_records(caplog)
    assert refused.audit["action"] == "create_session"
    assert refused.audit["result"] == "refused"


def test_a_local_caller_leaves_no_audit_line(e2e_repo, caplog):
    workflow, session = live(e2e_repo)
    caplog.set_level(logging.INFO, logger="winslow.audit")
    session.actions.submit(CheckTasks(keys=(by_name(workflow)["Alpha"].identity_key,)))
    assert audit_records(caplog) == []


def test_a_log_record_carries_the_credential_beside_the_user():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", (), None)
    with scoped_permissions(context("etl:view_task", user="can")):
        ContextStampFilter().filter(record)
    assert (record.user, record.credential) == ("can", "ticket")
    ContextStampFilter().filter(record)
    assert (record.user, record.credential) == (None, None)


def test_the_websocket_refuses_a_task_log_subscribe_without_view(e2e_repo):
    workflow, session = live(e2e_repo)
    registry = SessionRegistry()
    registry.register(session)
    app = create_app(
        registry,
        restricted("reports:view_task"),
        orchestrator=bare_orchestrator(),
        state_store=scratch_state_store(),
    )
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_json({"type": HelloFrame.type, "token": "t"})
        assert ws.receive_json()["type"] == HelloOkFrame.type
        ws.send_json(
            {
                "type": TaskLogSubscribeFrame.type,
                "session_id": session.session_id,
                "task_key": by_name(workflow)["Alpha"].identity_key,
                "request_id": "r-2",
            }
        )
        error = ws.receive_json()
        assert error["type"] == ErrorFrame.type
        assert "lacks my-workflow:view_task" in error["reason"]
