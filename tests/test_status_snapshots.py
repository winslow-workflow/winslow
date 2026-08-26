"""Snapshot writes: only terminal statuses persist through the listener, the
reads go through the persistence layer, and the session end archives them."""

import json

from winslow.constants import Mode
from winslow.session import Session
from winslow.task.status import SNAPSHOT_STATUSES

from harness import build_workflow, run_batch


def run_persisted(e2e_repo, state_store, mode):
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    workflow.init_state(state_store, origin="test")
    run_batch(workflow)
    return workflow, session


def snapshot_files(state_store, session):
    directory = state_store.open_directory / session.session_id / "tasks"
    return sorted(directory.glob("*.json"))


def test_only_terminal_statuses_persist(e2e_repo, state_store, mode):
    workflow, session = run_persisted(e2e_repo, state_store, mode)

    statuses = {
        json.loads(path.read_text())["status"]
        for path in snapshot_files(state_store, session)
    }
    assert statuses and statuses <= {status.name for status in SNAPSHOT_STATUSES}

    # The store keeps the last terminal transition of each task, which the
    # status history of the fixture store records. A task with no terminal
    # transition, for example the skipped one, never persists.
    snapshots = state_store.load_status_snapshots(session.session_id)
    expected = {
        key: terminal[-1].name
        for key, statuses in workflow.store.history.items()
        if (terminal := [s for s in statuses if s in SNAPSHOT_STATUSES])
    }
    assert {key: entry.status for key, entry in snapshots.items()} == expected
    assert "COMPLETED" in set(expected.values())
    assert "FAILED" in set(expected.values())


def test_load_snapshot_reads_what_the_listener_wrote(e2e_repo, state_store, mode):
    workflow, session = run_persisted(e2e_repo, state_store, mode)

    stored = state_store.load_status_snapshots(session.session_id)
    for task in workflow.tasks:
        assert workflow.load_snapshot(task.identity_key) == stored.get(task.identity_key)
    # The spectrum fixture leaves at least one task with no snapshot at all.
    assert any(
        workflow.load_snapshot(task.identity_key) is None for task in workflow.tasks
    )


def test_a_fresh_session_starts_with_zero_trust(e2e_repo, state_store, mode):
    # Snapshots are session-scoped: a new session of the same parameterization
    # inherits nothing, only a restore under the same id does.
    first, settled = run_persisted(e2e_repo, state_store, mode)
    second = build_workflow(e2e_repo, "my-workflow", mode)
    session = Session(second)
    second.init_state(state_store, origin="test")

    # The identity keys match across the sessions; only the id scoping keeps
    # the stored trust out.
    assert session.session_id != settled.session_id
    assert {t.identity_key for t in second.tasks} == {
        t.identity_key for t in first.tasks
    }
    assert state_store.load_status_snapshots(settled.session_id)
    assert all(second.load_snapshot(task.identity_key) is None for task in second.tasks)


def test_a_snapshot_write_failure_does_not_break_the_batch(
    e2e_repo, state_store, mode, monkeypatch
):
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    workflow.init_state(state_store, origin="test")

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(state_store, "save_status_snapshot", explode)
    run_batch(workflow)

    # The statuses landed although no snapshot persisted.
    statuses = set(workflow.store.values())
    assert statuses & {status for status in SNAPSHOT_STATUSES}
    assert state_store.load_status_snapshots(session.session_id) == {}

    # The dropped writes are counted, so flush and close can report them.
    listener = workflow.persistence_listener
    listener.flush()
    assert listener.write_failures > 0


def test_session_end_archives_the_snapshots(e2e_repo, state_store):
    workflow, session = run_persisted(e2e_repo, state_store, Mode.TUI)
    keys = set(state_store.load_status_snapshots(session.session_id))

    session.end()

    archive = state_store.ended_directory / session.session_id / "tasks"
    assert {path.stem for path in archive.glob("*.json")} == keys
    assert not (state_store.open_directory / session.session_id).exists()
