"""Restore: a second workflow instance under the same session id seeds its
store from the task snapshots, seeds an untrusted success as STALE, and
registers the batches that a dead process left open as INTERRUPTED."""

import time

import pytest

from winslow.events import Origin, TaskStatusEvent
from winslow.runner.execution import ExecutionStatus
from winslow.session import Session
from winslow.state import BatchRecord
from winslow.task.status import PASSING_STATUSES, SNAPSHOT_STATUSES, TaskStatus as S

from harness import build_workflow, by_name, run_batch


def start_session(e2e_repo, state_store, mode, session_id=None):
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    session = Session(workflow, session_id=session_id)
    workflow.check_pipeline_eligibility()
    workflow.init_state(state_store, origin="test")
    return workflow, session


def died_mid_flight(e2e_repo, state_store, mode):
    """A first session that ran and never ended, like a process death."""
    workflow, session = start_session(e2e_repo, state_store, mode)
    run_batch(workflow)
    return workflow, session


def test_restore_seeds_the_terminal_statuses(e2e_repo, state_store, mode):
    first, session = died_mid_flight(e2e_repo, state_store, mode)
    outcomes = dict(first.store.items())
    # A dead batch that names every task: a snapshot wins over the roster, and
    # a roster task with no snapshot stays ready.
    state_store.save_batch(
        BatchRecord(
            batch_uuid="dead-batch",
            session_id=session.session_id,
            action="RUN",
            created_at=time.time(),
            tasks={task.identity_key: str(task) for task in first.tasks},
        )
    )

    second, restored = start_session(
        e2e_repo, state_store, mode, session_id=session.session_id
    )
    second.seed_from_state()

    snapshots = state_store.load_status_snapshots(restored.session_id)
    for task in second.tasks:
        status = second.store[task]
        entry = snapshots.get(task.identity_key)
        if status is S.SKIPPED:
            continue
        if entry is not None:
            # No TTL: a success from before this session seeds as STALE.
            expected = S[entry.status]
            assert status is (
                S.STALE if expected in PASSING_STATUSES else expected
            )
        else:
            assert status is S.READY_TO_PROCESS
    # The seeded store carries both outcome kinds of the spectrum fixture.
    seeded = {second.store[task] for task in second.tasks}
    assert S.STALE in seeded and S.FAILED in seeded
    assert outcomes  # the first run really settled something


def test_a_seeded_success_is_stale_and_reprobes_on_first_touch(
    e2e_repo, state_store, mode
):
    first, session = died_mid_flight(e2e_repo, state_store, mode)
    second, restored = start_session(
        e2e_repo, state_store, mode, session_id=session.session_id
    )
    second.seed_from_state()
    alpha = by_name(second)["Alpha"]

    # No TTL: a seed from before this session seeds as STALE.
    assert second.store[alpha] is S.STALE

    run_batch(second, [alpha])

    # The STALE seed re-probed: the fresh target of this instance is empty,
    # so the probe failed, the task ran again, and the new snapshot is live.
    assert second.store[alpha] is S.COMPLETED
    assert alpha._has_been_run


def test_a_fresh_seed_inside_the_ttl_is_not_stale(
    e2e_repo, state_store, mode, monkeypatch
):
    first, session = died_mid_flight(e2e_repo, state_store, mode)
    second, restored = start_session(
        e2e_repo, state_store, mode, session_id=session.session_id
    )
    alpha = by_name(second)["Alpha"]
    monkeypatch.setattr(type(alpha), "check_ttl", 3600)
    second.seed_from_state()

    # Inside the TTL the seed is trusted: the real status seeds, not STALE.
    assert second.store[alpha] is S.COMPLETED

    run_batch(second, [alpha])

    # Inside the TTL the seed is trusted: the task did not run again.
    assert second.store[alpha] is S.COMPLETED
    assert not alpha._has_been_run


def test_open_batches_seed_as_interrupted(e2e_repo, state_store, mode):
    first, session = died_mid_flight(e2e_repo, state_store, mode)
    state_store.save_batch(
        BatchRecord(
            batch_uuid="dead-batch",
            session_id=session.session_id,
            action="RUN",
            created_at=time.time(),
            execution_options={
                "dry_run": True,
                "force_run": False,
                "force_success": False,
                "disable_concurrency": False,
            },
            tasks={"task-a": "A", "task-b": "B", "task-c": "C"},
        )
    )

    second, restored = start_session(
        e2e_repo, state_store, mode, session_id=session.session_id
    )
    second.seed_from_state()

    batch = second.runner.execution_batches_map["dead-batch"]
    assert batch.status is ExecutionStatus.INTERRUPTED
    assert batch.task_count == 3
    # The stored option snapshot restores the context of the history card.
    assert batch.execution_context.dry_run is True
    assert batch.completed_at is not None
    assert batch not in second.runner.active_batches
    # The record stamped INTERRUPTED, so a second restore does not seed it.
    assert state_store.load_open_batches(session.session_id) == []


def test_a_record_close_failure_does_not_break_the_restore(
    e2e_repo, state_store, mode
):
    first, session = died_mid_flight(e2e_repo, state_store, mode)
    state_store.save_batch(
        BatchRecord(
            batch_uuid="dead-batch",
            session_id=session.session_id,
            action="RUN",
            created_at=time.time(),
            tasks={"task-a": "A"},
        )
    )
    second, restored = start_session(
        e2e_repo, state_store, mode, session_id=session.session_id
    )

    def explode(*args, **kwargs):
        raise OSError("disk full")

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(state_store, "save_batch", explode)
        second.seed_from_state()

    # The batch seeded in memory and the session stays a restore candidate.
    batch = second.runner.execution_batches_map["dead-batch"]
    assert batch.status is ExecutionStatus.INTERRUPTED
    assert state_store.list_open_manifests()
    # Only the close stamp is lost: the next restore seeds the batch again.
    (open_record,) = state_store.load_open_batches(session.session_id)
    assert open_record.batch_uuid == "dead-batch"


def test_unsettled_tasks_of_a_dead_batch_stay_ready(e2e_repo, state_store, mode):
    # The first session died inside its first batch: nothing settled, so no
    # task has a snapshot, but the open record names the roster.
    first, session = start_session(e2e_repo, state_store, mode)
    roster = {task.identity_key: str(task) for task in first.tasks}
    state_store.save_batch(
        BatchRecord(
            batch_uuid="dead-batch",
            session_id=session.session_id,
            action="RUN",
            created_at=time.time(),
            tasks=roster,
        )
    )

    second, restored = start_session(
        e2e_repo, state_store, mode, session_id=session.session_id
    )
    second.seed_from_state()

    tasks = by_name(second)
    # The task pane shows the current state: a task with no snapshot is ready
    # to process again, and the INTERRUPTED batch in history tells the death
    # story. A rerun re-verifies through the normal pre-run check.
    assert second.store[tasks["Alpha"]] is S.READY_TO_PROCESS
    assert second.store[tasks["Ineligible"]] is S.SKIPPED
    batch = second.runner.execution_batches_map["dead-batch"]
    assert batch.status is ExecutionStatus.INTERRUPTED


def test_a_now_ineligible_task_is_not_restored(e2e_repo, state_store, mode):
    # Life one: the fixture's ineligible task is eligible, runs, and leaves a
    # COMPLETED snapshot behind. The patch is scoped to this life only.
    first = build_workflow(e2e_repo, "my-workflow", mode)
    ineligible = by_name(first)["Ineligible"]
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(type(ineligible), "is_eligible", lambda self: True)
        session = Session(first)
        first.check_pipeline_eligibility()
        first.init_state(state_store, origin="test")
        run_batch(first)
        assert first.store[ineligible] is S.COMPLETED
        assert ineligible.identity_key in state_store.load_status_snapshots(
            session.session_id
        )

    # Reality changed between the lives: the task is ineligible again.
    second, restored = start_session(
        e2e_repo, state_store, mode, session_id=session.session_id
    )
    second.seed_from_state()

    # The fresh eligibility pass is the golden source: the stored snapshot
    # does not resurrect the task.
    assert second.store[by_name(second)["Ineligible"]] is S.SKIPPED


def test_seed_writes_reach_the_other_listeners(e2e_repo, state_store, mode):
    first, session = died_mid_flight(e2e_repo, state_store, mode)
    second, restored = start_session(
        e2e_repo, state_store, mode, session_id=session.session_id
    )
    seen = {}
    origins = set()

    def record(event):
        seen[event.key] = event.status
        origins.add(event.origin)

    second.bus.subscribe(TaskStatusEvent, record)
    second.seed_from_state()

    # Every replayed snapshot reached the subscriber as a normal store event,
    # stamped SEED; the persistence subscriber skips that origin itself.
    snapshots = state_store.load_status_snapshots(session.session_id)
    assert set(seen) == set(snapshots)
    assert origins == {Origin.SEED}
    for key, entry in snapshots.items():
        expected = S[entry.status]
        if expected in PASSING_STATUSES:
            expected = S.STALE
        assert seen[key] is expected


def test_seeding_writes_nothing_back_to_the_snapshots(e2e_repo, state_store, mode):
    first, session = died_mid_flight(e2e_repo, state_store, mode)
    before = state_store.load_status_snapshots(session.session_id)

    second, restored = start_session(
        e2e_repo, state_store, mode, session_id=session.session_id
    )
    second.seed_from_state()

    assert state_store.load_status_snapshots(session.session_id) == before
    assert set(before.values()) and all(
        entry.status in {s.name for s in SNAPSHOT_STATUSES} for entry in before.values()
    )
