"""The permission check of the port: the decorators on the handlers and the
reads, the two handlers that refine their gate from the session state, the
filtered list reads, the subscribe check of a connection, and the doors that
carry a refusal as data. A hand-built context stands in for the scoped
authenticator of the later steps."""

import logging

import pytest
from starlette.testclient import TestClient

from winslow.actions import ActionHandler
from winslow.session import AppActionHandler, Session, SessionRegistry
from winslow.client import LocalAppClient, LocalSessionClient
from winslow.client.base import Action, AppClient, Read, SessionClient
from winslow.constants import Mode
from winslow.exceptions import RequestError
from winslow.orchestrator import Orchestrator, OrchestratorConfig
from winslow.access.permissions import (
    CredentialKind,
    Permission,
    PermissionContext,
    expand,
)
from winslow.logger import ContextStampFilter
from winslow.protocol.frames import (
    ErrorFrame,
    HelloFrame,
    HelloOkFrame,
    SubscribeFrame,
    TaskLogSubscribeFrame,
)
from winslow.serve import create_app
from winslow.task.context import scoped_permissions
from winslow.task.status import TaskStatus as S

from harness import (
    started,
    bare_orchestrator,
    build_workflow,
    by_name,
    fixed_authenticator,
    run_all,
    scratch_state_store,
    start_gated_batch,
    wait_for_status,
)
from test_serve_mcp import call_tools, mcp_app

P = Permission


run_tasks = SessionClient.run_tasks
check_tasks = SessionClient.check_tasks
stop_batch = SessionClient.stop_batch
end_session = SessionClient.end_session


def context(*spelled, user="lucas", credential_id=None):
    return PermissionContext(
        user=user,
        scopes=expand(spelled),
        credential=CredentialKind.TICKET,
        credential_id=credential_id,
    )


def live(e2e_repo, name="my-workflow"):
    workflow = build_workflow(e2e_repo, name, Mode.TUI)
    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    return workflow, session


def restricted(*spelled):
    return fixed_authenticator(context(*spelled))


# --- completeness -----------------------------------------------------------------


def test_every_action_names_its_permissions_and_has_its_handler():
    for name, spec in SessionClient.action_meta.items():
        assert spec.requires, name
        assert callable(getattr(ActionHandler, name)), name
    for name, spec in AppClient.action_meta.items():
        assert spec.requires, name
        assert callable(getattr(AppActionHandler, name)), name
        assert callable(getattr(AppActionHandler, f"{name}_target")), name
    with pytest.raises(TypeError, match="names its permissions"):
        Action(keys=tuple)


def test_bind_refuses_a_surplus_positional_and_a_field_named_twice():
    with pytest.raises(TypeError, match="takes 2 arguments"):
        SessionClient.run_tasks("k", {"x": 1}, "extra")
    with pytest.raises(TypeError, match="both positional and by name"):
        SessionClient.run_tasks("k", keys=("k",))


def test_a_read_names_exactly_one_check():
    for port in (AppClient, SessionClient):
        for name, spec in port.read_meta.items():
            declared = (spec.requires, spec.visible, spec.open)
            assert sum(1 for d in declared if d) == 1, name
    assert [n for n, s in AppClient.read_meta.items() if s.open] == ["host_stats"]
    with pytest.raises(TypeError, match="exactly one"):
        Read(result=dict)
    with pytest.raises(TypeError, match="exactly one"):
        Read(result=dict, requires=Permission.VIEW_TASK, open=True)


# --- the handlers -----------------------------------------------------------------


def test_a_local_caller_passes_everything(e2e_repo):
    workflow, session = live(e2e_repo)
    ack = session.actions.submit(
        check_tasks(keys=(by_name(workflow)["Alpha"].identity_key,))
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
            run_tasks(keys=(alpha.identity_key,)),
            check_tasks(keys=(alpha.identity_key,)),
            end_session(force=False),
        ):
            ack = session.actions.submit(action)
            assert ack.accepted is False
            assert "lucas lacks my-workflow:" in ack.reason
            assert "the scopes cover my-workflow:view_task" in ack.reason


def test_check_submits_a_check_batch_and_no_run_batch(e2e_repo):
    workflow, session = live(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    with scoped_permissions(context("my-workflow:check_task")):
        refused = session.actions.submit(run_tasks(keys=(alpha.identity_key,)))
        assert refused.accepted is False
        assert "lacks my-workflow:run_task" in refused.reason
        ack = session.actions.submit(check_tasks(keys=(alpha.identity_key,)))
        assert ack.accepted


def test_run_implies_check(e2e_repo):
    workflow, session = live(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    with scoped_permissions(context("my-workflow:run_task")):
        assert session.actions.submit(check_tasks(keys=(alpha.identity_key,))).accepted
        assert session.actions.submit(run_tasks(keys=(alpha.identity_key,))).accepted
    wait_for_status(workflow, alpha, S.COMPLETED)


def test_a_scope_on_another_name_does_not_reach_this_session(e2e_repo):
    workflow, session = live(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    with scoped_permissions(context("reports:run_task")):
        ack = session.actions.submit(check_tasks(keys=(alpha.identity_key,)))
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
            ack = session.actions.submit(stop_batch(batch_uuid=batch.uuid))
        assert ack.accepted is False
        assert "a run batch stops on my-gates:run_task alone" in ack.reason
        with scoped_permissions(context("my-gates:run_task")):
            ack = session.actions.submit(stop_batch(batch_uuid=batch.uuid))
        assert ack.accepted
    finally:
        gate.set()
        batch.wait()


def test_check_ends_a_settled_session_and_not_an_unsettled_one(e2e_repo):
    # my-depends: every task passes, so a full run settles the session.
    workflow, session = live(e2e_repo, "my-depends")
    with scoped_permissions(context("my-depends:check_task")):
        ack = session.actions.submit(end_session(force=False))
    assert ack.accepted is False
    assert "not settled - ending needs my-depends:end_workflow" in ack.reason
    assert str(session.unsettled_count) in ack.reason

    run_all(workflow)
    assert session.unsettled_count == 0
    with scoped_permissions(context("my-depends:check_task")):
        ack = session.actions.submit(end_session(force=False))
    assert ack.accepted
    assert session.has_ended


def test_end_workflow_ends_an_unsettled_session(e2e_repo):
    workflow, session = live(e2e_repo)
    with scoped_permissions(context("my-workflow:end_workflow")):
        assert session.actions.submit(end_session(force=False)).accepted


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
    info = started(port.create_session("my-workflow"))
    other = started(port.create_session("my-gates"))
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
        refused = port.create_session("my-workflow")
        assert "lacks my-workflow:start_workflow" in refused.reason
    with scoped_permissions(context("my-workflow:start_workflow")):
        info = started(port.create_session("my-workflow"))
    registry.remove(info.session_id)
    with scoped_permissions(context("my-workflow:view_task")):
        refused = port.restore_session(info.session_id)
        assert "lacks my-workflow:start_workflow" in refused.reason
    with scoped_permissions(context("my-workflow:start_workflow")):
        restored = started(port.restore_session(info.session_id))
        assert restored.session_id == info.session_id


def test_a_restore_refusal_reveals_nothing_to_a_caller_without_the_scope(
    e2e_repo, state_store
):
    """The check runs before the refusal that names what exists: an unknown
    id, a live session, an uncollected workflow all read as a missing scope."""
    registry = SessionRegistry()
    port = LocalAppClient(
        registry,
        orchestrator=collecting_orchestrator(e2e_repo),
        state_store=state_store,
    )
    info = started(port.create_session("my-workflow"))
    with scoped_permissions(context("reports:view_task")):
        live = port.restore_session(info.session_id)
        assert "lacks my-workflow:start_workflow" in live.reason
        unknown = port.restore_session("gone")
        assert "lacks gone:start_workflow" in unknown.reason
        assert "manifest" not in unknown.reason
    registry.remove(info.session_id)
    with scoped_permissions(context("*:start_workflow")):
        assert "no open manifest" in port.restore_session("gone").reason


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
    with scoped_permissions(
        context("my-workflow:check_task", user="lucas", credential_id="4c1f")
    ):
        LocalSessionClient(session).snapshot()
        assert audit_records(caplog) == []
        session.actions.submit(check_tasks(keys=(alpha.identity_key,)))
        session.actions.submit(run_tasks(keys=(alpha.identity_key,)))
    accepted, refused = audit_records(caplog)
    assert accepted.audit == {
        "action": "check_tasks",
        "scope": "my-workflow",
        "session": session.session_id,
        "keys": 1,
        "result": "accepted",
        "reason": None,
    }
    assert (
        "user=lucas credential=ticket credential_id=4c1f action=check_tasks"
        in accepted.getMessage()
    )
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
        assert port.create_session("my-workflow").accepted is False
    (refused,) = audit_records(caplog)
    assert refused.audit["action"] == "create_session"
    assert refused.audit["scope"] == "my-workflow"
    assert refused.audit["result"] == "refused"


def test_a_start_audits_acceptance_and_a_refusal_from_the_init(
    e2e_repo, state_store, caplog
):
    port = LocalAppClient(
        SessionRegistry(),
        orchestrator=collecting_orchestrator(e2e_repo),
        state_store=state_store,
    )
    caplog.set_level(logging.INFO, logger="winslow.audit")
    with scoped_permissions(context("*:start_workflow")):
        info = started(port.create_session("my-workflow"))
        nope = port.create_session("nope")
        assert "nope" in nope.reason and "Traceback" in nope.detail
    accepted, refused = audit_records(caplog)
    assert accepted.audit == {
        "action": "create_session",
        "scope": "my-workflow",
        "session": info.session_id,
        "keys": None,
        "result": "accepted",
        "reason": None,
    }
    assert refused.audit["action"] == "create_session"
    assert refused.audit["result"] == "refused"
    assert "nope" in refused.audit["reason"]


def test_a_refused_restore_audits_the_session_id_as_given(
    e2e_repo, state_store, caplog
):
    port = LocalAppClient(
        SessionRegistry(),
        orchestrator=collecting_orchestrator(e2e_repo),
        state_store=state_store,
    )
    caplog.set_level(logging.INFO, logger="winslow.audit")
    with scoped_permissions(context("*:start_workflow")):
        assert port.restore_session("gone").accepted is False
    (refused,) = audit_records(caplog)
    assert refused.audit["action"] == "restore_session"
    assert refused.audit["session"] == "gone"
    assert refused.audit["result"] == "refused"


def test_an_ended_session_and_a_broken_handler_leave_a_refused_line(
    e2e_repo, caplog, monkeypatch
):
    workflow, session = live(e2e_repo)
    caplog.set_level(logging.INFO, logger="winslow.audit")

    def explode(self, action):
        raise RuntimeError("boom")

    monkeypatch.setattr(ActionHandler, "check_tasks", explode)
    with scoped_permissions(context("my-workflow:*")):
        ack = session.actions.submit_guarded(check_tasks(keys=("k",)))
        assert "the session log has the traceback" in ack.reason
        session.actions.submit(end_session())
        assert session.actions.submit(check_tasks(keys=("k",))).accepted is False
    broken, ended, after = audit_records(caplog)
    assert broken.audit["action"] == "check_tasks"
    assert broken.audit["result"] == "refused"
    assert "the session log" in broken.audit["reason"]
    assert ended.audit["action"] == "end_session"
    assert ended.audit["result"] == "accepted"
    assert after.audit["result"] == "refused"
    assert "has ended" in after.audit["reason"]


def test_a_broken_app_resolver_leaves_a_refused_line_with_the_id_as_given(
    e2e_repo, state_store, caplog, monkeypatch
):
    port = LocalAppClient(
        SessionRegistry(),
        orchestrator=collecting_orchestrator(e2e_repo),
        state_store=state_store,
    )
    caplog.set_level(logging.INFO, logger="winslow.audit")

    def explode(self, action):
        raise RuntimeError("boom")

    monkeypatch.setattr(AppActionHandler, "restore_session_target", explode)
    with scoped_permissions(context("*:start_workflow")):
        ack = port.actions.submit_guarded(AppClient.restore_session("gone"))
    assert ack.accepted is False
    assert "the server log has the traceback" in ack.reason
    (refused,) = audit_records(caplog)
    assert refused.audit == {
        "action": "restore_session",
        "scope": None,
        "session": "gone",
        "keys": None,
        "result": "refused",
        "reason": ack.reason,
    }


def test_a_local_caller_leaves_no_audit_line(e2e_repo, caplog):
    workflow, session = live(e2e_repo)
    caplog.set_level(logging.INFO, logger="winslow.audit")
    session.actions.submit(check_tasks(keys=(by_name(workflow)["Alpha"].identity_key,)))
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
