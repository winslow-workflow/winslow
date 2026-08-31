"""The manifest lifecycle: a session with a state store writes a manifest once
its pipeline is runnable and archives it at the end."""

import pytest

from winslow.constants import Mode
from winslow.events import TaskStatusEvent
from winslow.exceptions import RegistrationError
from winslow.orchestrator import Orchestrator, OrchestratorConfig
from winslow.session import Session, SessionRegistry
from winslow.state import StaleSweeper

from harness import build_workflow


def persisted_session(e2e_repo, state_store, **state_kwargs):
    workflow = build_workflow(e2e_repo, "my-workflow", Mode.TUI)
    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    workflow.init_state(state_store, origin="tui", **state_kwargs)
    return workflow, session


def test_creation_writes_an_open_manifest(e2e_repo, state_store):
    workflow, session = persisted_session(
        e2e_repo, state_store, workflow_values={"region": "emea"}
    )
    (manifest,) = state_store.list_open_manifests()
    assert manifest.session_id == session.session_id
    assert manifest.workflow_class == type(workflow).get_name()
    assert manifest.workflow_namespace == workflow.cache_namespace
    assert manifest.workflow_values == {"region": "emea"}
    assert manifest.origin == "tui"
    assert manifest.ended_at is None


def test_a_clean_end_leaves_no_open_manifest(e2e_repo, state_store):
    workflow, session = persisted_session(e2e_repo, state_store)
    workflow.runner.bulk_run(workflow.tasks)
    session.end()
    assert state_store.list_open_manifests() == []
    assert (state_store.ended_directory / session.session_id).is_dir()


def test_an_errored_session_archives_as_failed(e2e_repo, state_store):
    workflow, session = persisted_session(e2e_repo, state_store)
    session.mark_error()
    assert state_store.list_open_manifests() == []
    assert (state_store.error_directory / session.session_id).is_dir()
    assert not (state_store.ended_directory / session.session_id).exists()


def test_a_session_without_a_store_persists_nothing(e2e_repo):
    workflow = build_workflow(e2e_repo, "my-workflow", Mode.TUI)
    session = Session(workflow)
    session.end()
    assert workflow.persistence_listener is None


def test_a_second_init_state_registers_nothing(e2e_repo, state_store):
    workflow, session = persisted_session(e2e_repo, state_store)
    listener = workflow.persistence_listener
    sweeper = workflow.stale_sweeper

    workflow.init_state(state_store, origin="tui")

    assert workflow.persistence_listener is listener
    assert workflow.stale_sweeper is sweeper
    subscribed = [callback for _, callback in workflow.bus._receivers]
    assert subscribed.count(listener.on_task_status) == 1
    assert subscribed.count(sweeper.on_task_status) == 1


def test_the_bus_refuses_a_duplicate_subscription(e2e_repo, state_store):
    workflow, session = persisted_session(e2e_repo, state_store)
    listener = workflow.persistence_listener

    with pytest.raises(RegistrationError):
        workflow.bus.subscribe(TaskStatusEvent, listener.on_task_status)


def _explode(*args, **kwargs):
    raise OSError("disk full")


def test_a_registration_failure_leaves_no_open_manifest(
    e2e_repo, state_store, monkeypatch
):
    # The sweeper registers after the adapter and before the manifest save:
    # its failure must undo the adapter and leave no restore candidate.
    monkeypatch.setattr(StaleSweeper, "__init__", _explode)
    workflow = build_workflow(e2e_repo, "my-workflow", Mode.TUI)
    Session(workflow)
    workflow.init_state(state_store, origin="tui")

    assert state_store.list_open_manifests() == []
    assert workflow.persistence_listener is None
    assert workflow.stale_sweeper is None
    assert workflow.bus._receivers == {}


def test_a_manifest_write_failure_degrades_to_a_non_persistent_session(
    e2e_repo, state_store, monkeypatch
):
    monkeypatch.setattr(state_store, "save_manifest", _explode)
    workflow = build_workflow(e2e_repo, "my-workflow", Mode.TUI)
    session = Session(workflow)
    workflow.init_state(state_store, origin="tui")

    assert workflow.persistence_listener is None

    # The degraded session still runs and ends.
    workflow.check_pipeline_eligibility()
    workflow.runner.bulk_run(workflow.tasks)
    session.end()
    assert session.has_ended


def test_an_end_persistence_failure_does_not_break_the_end(
    e2e_repo, state_store, monkeypatch
):
    workflow, session = persisted_session(e2e_repo, state_store)

    monkeypatch.setattr(state_store, "mark_ended", _explode)
    session.end()
    assert session.has_ended


def test_serve_startup_creates_the_auto_init_sessions(
    e2e_repo, state_store, monkeypatch
):
    """auto_init is the duty of the session owner: the serve startup creates
    the session, so a connecting client never starts a second one."""
    orchestrator = _serve_orchestrator(e2e_repo)
    monkeypatch.setattr(
        orchestrator.workflow_registry["my-workflow"], "auto_init", True
    )
    registry = SessionRegistry()

    orchestrator._auto_init_sessions(registry, state_store)

    (session,) = registry.sessions()
    assert session.workflow.instance_name == "my-workflow"
    assert session.status.name == "ACTIVE"
    (manifest,) = state_store.list_open_manifests()
    assert manifest.session_id == session.session_id


def _serve_orchestrator(e2e_repo):
    config, unknown = Orchestrator.get_base_parser().parse_known_args(
        ["serve"], namespace=OrchestratorConfig()
    )
    orchestrator = Orchestrator(config, directory=e2e_repo, unknown_args=unknown)
    orchestrator.workflow_registry.collect_classes(e2e_repo)
    return orchestrator


def test_serve_startup_restores_the_open_manifests(e2e_repo, state_store):
    """The sessions of a dead serve process come back at the next startup,
    the way a local user's restore brings them back."""
    from winslow.client import LocalAppClient

    orchestrator = _serve_orchestrator(e2e_repo)
    dead = LocalAppClient(
        SessionRegistry(), orchestrator=orchestrator, state_store=state_store
    )
    row = dead.create_session("my-workflow")

    registry = SessionRegistry()
    orchestrator._restore_sessions(registry, state_store)

    (session,) = registry.sessions()
    assert session.session_id == row.session_id


def test_a_restored_session_satisfies_auto_init(e2e_repo, state_store, monkeypatch):
    from winslow.client import LocalAppClient

    orchestrator = _serve_orchestrator(e2e_repo)
    monkeypatch.setattr(
        orchestrator.workflow_registry["my-workflow"], "auto_init", True
    )
    dead = LocalAppClient(
        SessionRegistry(), orchestrator=orchestrator, state_store=state_store
    )
    row = dead.create_session("my-workflow")

    registry = SessionRegistry()
    orchestrator._restore_sessions(registry, state_store)
    orchestrator._auto_init_sessions(registry, state_store)

    (session,) = registry.sessions()
    assert session.session_id == row.session_id
