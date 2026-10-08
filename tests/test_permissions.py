"""The permission model: the implication table inside expand, the wildcards,
the PermissionContext every credential builds, the scope name of a workflow class,
and the rows and frames that carry it."""

import pytest
from starlette.testclient import TestClient

from winslow import Workflow
from winslow.client import LocalAppClient
from winslow.constants import Mode
from winslow.orchestrator import Orchestrator, OrchestratorConfig
from winslow.permissions import (
    ALL,
    CredentialKind,
    Permission,
    PermissionContext,
    Scope,
    expand,
    parse_scopes,
    wire_scopes,
)
from winslow.protocol.frames import HelloFrame, HelloOkFrame, RequestFrame, ResultFrame
from winslow.serve import create_app
from winslow.session import Session, SessionRegistry
from winslow.task.context import current_permissions, current_user, scoped_permissions

from harness import TOKEN, serve_credentials
from harness import bare_orchestrator, build_workflow, scratch_state_store

P = Permission


def scopes(*pairs):
    return {Scope(*pair) for pair in pairs}


# --- expand and allows ------------------------------------------------------------


def test_run_implies_check_and_view():
    assert expand({"etl:run_task"}) == scopes(
        ("etl", P.RUN_TASK), ("etl", P.CHECK_TASK), ("etl", P.VIEW_TASK)
    )


def test_check_implies_view_alone():
    assert expand({"etl:check_task"}) == scopes(
        ("etl", P.CHECK_TASK), ("etl", P.VIEW_TASK)
    )


def test_start_and_end_imply_view_and_nothing_else():
    assert expand({"etl:start_workflow"}) == scopes(
        ("etl", P.START_WORKFLOW), ("etl", P.VIEW_TASK)
    )
    assert expand({"etl:end_workflow"}) == scopes(
        ("etl", P.END_WORKFLOW), ("etl", P.VIEW_TASK)
    )


def test_the_permission_wildcard_is_the_five():
    assert expand({"etl:*"}) == {Scope("etl", p) for p in Permission}


def test_the_scope_wildcard_covers_every_scope_name():
    context = PermissionContext(
        user="can", scopes=expand({"*:view_task"}), credential=CredentialKind.TICKET
    )
    assert context.allows("etl", P.VIEW_TASK)
    assert context.allows("reports", P.VIEW_TASK)
    assert not context.allows("etl", P.CHECK_TASK)


def test_all_covers_everything():
    assert ALL == {Scope("*", p) for p in Permission}
    context = PermissionContext.full("can", "test")
    assert all(context.allows(name, p) for name in ("etl", "x") for p in Permission)


def test_a_scope_does_not_reach_another_name():
    context = PermissionContext(
        user="can", scopes=expand({"etl:run_task"}), credential=CredentialKind.TICKET
    )
    assert context.allows("etl", P.RUN_TASK)
    assert not context.allows("reports", P.RUN_TASK)
    assert not context.allows("reports", P.VIEW_TASK)


def test_until_keeps_the_earlier_expiry():
    assert PermissionContext.full("can", "shared").until(100.0).expires_at == 100.0
    assert (
        PermissionContext.full("can", "token", 500.0).until(100.0).expires_at == 100.0
    )
    assert PermissionContext.full("can", "ticket", 50.0).until(100.0).expires_at == 50.0


def test_the_wire_spelling_round_trips():
    spelled = wire_scopes(expand({"etl:run_task", "*:view_task"}))
    assert spelled == ("*:view_task", "etl:check_task", "etl:run_task", "etl:view_task")
    assert frozenset(s for word in spelled for s in parse_scopes(word)) == expand(
        spelled
    )


def test_a_malformed_scope_refuses_with_the_shape():
    with pytest.raises(ValueError, match="<scope name>:<permission>"):
        parse_scopes("etl")
    with pytest.raises(ValueError, match="names no permission"):
        expand({"etl:fly"})


# --- the ambient scope ------------------------------------------------------------


def test_current_user_reads_the_context():
    assert current_permissions() is None
    assert current_user() is None
    with scoped_permissions(PermissionContext.full("can", "test")):
        assert current_user() == "can"
        assert current_permissions().allows("etl", P.END_WORKFLOW)
    assert current_user() is None


# --- the scope name of a workflow -------------------------------------------------


def test_the_scope_name_defaults_to_the_workflow_name():
    class Etl(Workflow):
        pass

    assert Etl.get_permission_scope() == Etl.get_name() == "etl"


def test_a_base_class_sets_the_scope_name_for_its_family():
    class FuturesBase(Workflow):
        permission_scope = "Futures"

        class Meta:
            abstract = True

    class FuturesRollout(FuturesBase):
        pass

    class FuturesRollback(FuturesBase):
        pass

    assert FuturesRollout.get_permission_scope() == "Futures"
    assert FuturesRollback.get_permission_scope() == "Futures"
    context = PermissionContext(
        user="can",
        scopes=expand({"Futures:run_task"}),
        credential=CredentialKind.TICKET,
    )
    assert context.allows(FuturesRollout.get_permission_scope(), P.RUN_TASK)
    assert context.allows(FuturesRollback.get_permission_scope(), P.RUN_TASK)


# --- the rows and the frames ------------------------------------------------------


def app_for(registry, orchestrator=None, state_store=None):
    return create_app(
        registry,
        serve_credentials(),
        hello_timeout=1.0,
        orchestrator=orchestrator or bare_orchestrator(),
        state_store=state_store or scratch_state_store(),
    )


def collecting_orchestrator(directory):
    config, unknown = Orchestrator.get_base_parser().parse_known_args(
        ["serve"], namespace=OrchestratorConfig()
    )
    orchestrator = Orchestrator(config, directory=directory, unknown_args=unknown)
    orchestrator.workflow_registry.collect_classes(directory)
    return orchestrator


def request(ws, kind, **fields):
    ws.send_json(
        {"type": RequestFrame.type, "request_id": "r-1", "kind": kind, **fields}
    )
    result = ws.receive_json()
    assert result["type"] == ResultFrame.type
    return result["result"]


def test_the_session_row_and_the_local_client_carry_the_scope_name(e2e_repo):
    workflow = build_workflow(e2e_repo, "my-workflow", Mode.TUI)
    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    registry = SessionRegistry()
    registry.register(session)

    port = LocalAppClient(
        registry, orchestrator=bare_orchestrator(), state_store=scratch_state_store()
    )
    (row,) = port.sessions()
    assert row.permission_scope == type(workflow).get_permission_scope()
    assert port.permissions().scopes == ALL
    assert port.permissions().user == "local"

    with TestClient(app_for(registry)).websocket_connect("/ws") as ws:
        ws.send_json({"type": HelloFrame.type, "token": TOKEN})
        hello_ok = ws.receive_json()
        assert hello_ok["type"] == HelloOkFrame.type
        assert hello_ok["scopes"] == list(wire_scopes(ALL))
        (served,) = request(ws, "sessions")
        assert served["permission_scope"] == row.permission_scope


def test_the_descriptors_and_the_manifests_carry_the_scope_name(e2e_repo, state_store):
    orchestrator = collecting_orchestrator(e2e_repo)
    registry = SessionRegistry()
    port = LocalAppClient(registry, orchestrator=orchestrator, state_store=state_store)
    info = port.create_session("my-workflow")
    # The process dies: the manifest stays open with no live session.
    registry.remove(info.session_id)

    with TestClient(app_for(registry, orchestrator, state_store)).websocket_connect(
        "/ws"
    ) as ws:
        ws.send_json({"type": HelloFrame.type, "token": TOKEN})
        ws.receive_json()
        descriptors = request(ws, "descriptors")
        by_name = {
            d["workflow"]: d["permission_scope"] for d in descriptors["workflows"]
        }
        assert by_name["my-workflow"] == "my-workflow"
        assert all(by_name.values())
        (manifest,) = request(ws, "manifests")
        assert manifest["permission_scope"] == "my-workflow"
