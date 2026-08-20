"""The manifest lifecycle: a session with a state store writes a manifest once
its pipeline is runnable, updates it on option changes, and archives it at the
end."""

import pytest

from winslow.constants import Mode
from winslow.exceptions import RegistrationError
from winslow.orchestrator import Orchestrator, OrchestratorConfig
from winslow.session import Session
from winslow.state import SessionPersistenceAdapter, StaleSweeper

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
    workflow.runner.bulk_run(workflow.runner.eligible_tasks(workflow.tasks))
    session.end()
    assert state_store.list_open_manifests() == []
    assert (state_store.ended_directory / session.session_id).is_dir()


def test_an_errored_session_archives_as_failed(e2e_repo, state_store):
    workflow, session = persisted_session(e2e_repo, state_store)
    session.mark_error()
    assert state_store.list_open_manifests() == []
    assert (state_store.error_directory / session.session_id).is_dir()
    assert not (state_store.ended_directory / session.session_id).exists()


def test_option_changes_update_the_manifest(e2e_repo, state_store):
    workflow, session = persisted_session(
        e2e_repo, state_store, orchestrator_overrides={"filter": "gate"}
    )
    workflow.batch_options.dry_run = True
    workflow.record_batch_options()

    (manifest,) = state_store.list_open_manifests()
    assert manifest.orchestrator_overrides["dry_run"] is True
    # The original overrides survive the fold.
    assert manifest.orchestrator_overrides["filter"] == "gate"
    assert manifest.session_id == session.session_id


def test_recorded_options_rebuild_the_workflow(e2e_repo, state_store):
    # The restore leg of the option round trip, minus the UI: the manifest
    # overrides land on the orchestrator config, which builds the options.
    workflow, session = persisted_session(e2e_repo, state_store)
    workflow.batch_options.dry_run = True
    workflow.record_batch_options()
    (manifest,) = state_store.list_open_manifests()

    parser = Orchestrator.get_base_parser()
    config = parser.parse_args(
        ["run", "--mode", Mode.TUI.value, "--workflow", "my-workflow"],
        namespace=OrchestratorConfig(),
    )
    orchestrator = Orchestrator(config, directory=e2e_repo)
    orchestrator.workflow_registry.collect_classes(e2e_repo)
    rebuilt = orchestrator.initialize_workflow(
        workflow_kls=orchestrator.workflow_registry["my-workflow"],
        orchestrator_overrides=manifest.orchestrator_overrides,
        workflow_values=manifest.workflow_values or {},
    )
    assert rebuilt.batch_options.dry_run is True
    assert rebuilt.batch_options.force_run is False


def test_a_session_without_a_store_persists_nothing(e2e_repo):
    workflow = build_workflow(e2e_repo, "my-workflow", Mode.TUI)
    session = Session(workflow)
    # The persistence hooks are no-ops without a store.
    workflow.record_batch_options()
    session.end()
    assert workflow.persistence_listener is None
    assert workflow.state_store is None


def test_a_second_init_state_registers_nothing(e2e_repo, state_store):
    workflow, session = persisted_session(e2e_repo, state_store)
    listener = workflow.persistence_listener
    sweeper = workflow.stale_sweeper

    workflow.init_state(state_store, origin="tui")

    assert workflow.persistence_listener is listener
    assert workflow.stale_sweeper is sweeper
    kinds = (SessionPersistenceAdapter, StaleSweeper)
    for kind in kinds:
        assert sum(isinstance(lst, kind) for lst in workflow.store.listeners) == 1


def test_the_listener_slots_refuse_a_second_listener(e2e_repo, state_store):
    workflow, session = persisted_session(e2e_repo, state_store)
    adapter = SessionPersistenceAdapter(state_store, session.session_id)
    sweeper = StaleSweeper(workflow)

    try:
        with pytest.raises(RegistrationError):
            workflow.persistence_listener = adapter
        with pytest.raises(RegistrationError):
            workflow.stale_sweeper = sweeper
    finally:
        adapter.close()
        sweeper.close()


def _explode(*args, **kwargs):
    raise OSError("disk full")


def test_a_registration_failure_leaves_no_open_manifest(
    e2e_repo, state_store, monkeypatch
):
    # The sweeper registers after the adapter and before the manifest save:
    # its failure must undo the adapter and leave no restore candidate.
    monkeypatch.setattr(StaleSweeper, "__init__", _explode)
    workflow = build_workflow(e2e_repo, "my-workflow", Mode.TUI)
    session = Session(workflow)
    workflow.init_state(state_store, origin="tui")

    assert state_store.list_open_manifests() == []
    assert workflow.persistence_listener is None
    assert workflow.stale_sweeper is None
    assert workflow.store.listeners == ()


def test_a_manifest_write_failure_degrades_to_a_non_persistent_session(
    e2e_repo, state_store, monkeypatch
):
    monkeypatch.setattr(state_store, "save_manifest", _explode)
    workflow = build_workflow(e2e_repo, "my-workflow", Mode.TUI)
    session = Session(workflow)
    workflow.init_state(state_store, origin="tui")

    assert workflow.state_store is None
    assert workflow.persistence_listener is None

    # The degraded session still runs and ends.
    workflow.check_pipeline_eligibility()
    workflow.runner.bulk_run(workflow.runner.eligible_tasks(workflow.tasks))
    session.end()
    assert session.has_ended


def test_an_option_save_failure_does_not_break_the_toggle(
    e2e_repo, state_store, monkeypatch
):
    workflow, session = persisted_session(e2e_repo, state_store)

    monkeypatch.setattr(state_store, "save_manifest", _explode)
    workflow.batch_options.dry_run = True
    workflow.record_batch_options()

    # The stored manifest keeps its creation state.
    (manifest,) = state_store.list_open_manifests()
    assert manifest.orchestrator_overrides is None


def test_an_end_persistence_failure_does_not_break_the_end(
    e2e_repo, state_store, monkeypatch
):
    workflow, session = persisted_session(e2e_repo, state_store)

    monkeypatch.setattr(state_store, "mark_ended", _explode)
    session.end()
    assert session.has_ended
