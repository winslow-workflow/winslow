"""Interactive-only, like the session lifecycle tests: request_stop is the
History tab's stop button, and the stop machinery exists on the interactive
runner. The point under test: a stop belongs to its batch - the session and
the tasks both outlive it."""

from winslow.runner.execution import ExecutionStatus
from winslow.session import SessionStatus
from winslow.task.status import TaskStatus as S

from harness import (
    RERUN_LADDER,
    STOPPED_MID_RUN,
    SWEPT,
    gated_workflow,
    start_gated_batch,
)


def stopped_batch(e2e_repo):
    """A gated batch stopped mid-run and drained: Gated finishes its run
    (stop is cooperative) but is ABORTED, the tails are swept unstarted."""
    workflow, tasks = gated_workflow(e2e_repo, "--disable-concurrency")
    gate, batch = start_gated_batch(workflow, tasks)
    batch.request_stop()
    gate.set()
    batch.wait()
    return workflow, tasks, batch


def test_stop_aborts_batch_and_spares_session(e2e_repo):
    """request_stop without end(): the batch lands on STOPPED with the
    force_end task ladders - but the session stays ACTIVE with its store
    intact, because stopping a batch says nothing about the session."""
    workflow, tasks, batch = stopped_batch(e2e_repo)

    assert batch.status is ExecutionStatus.STOPPED
    assert workflow.session.status is SessionStatus.ACTIVE
    assert not workflow.session.is_ending
    assert len(workflow.store) == 3

    workflow.store.assert_history_equals(tasks["Gated"], STOPPED_MID_RUN)
    assert workflow.target[tasks["Gated"]] is True
    for name in ("TailOne", "TailTwo"):
        workflow.store.assert_history_equals(tasks[name], SWEPT)
        assert tasks[name] not in workflow.target


def test_aborted_tasks_rerun_in_a_fresh_batch(e2e_repo):
    """ABORTED is not sticky: a fresh run batch takes the swept tasks to
    COMPLETED, and the stopped task's landed work satisfies its re-check
    without a second run - the stop belonged to the batch, not the tasks."""
    workflow, tasks, _ = stopped_batch(e2e_repo)

    rerun = workflow.runner.submit_run(workflow.tasks)
    rerun.wait()

    assert rerun.status is ExecutionStatus.FINISHED
    workflow.store.assert_history_equals(
        tasks["Gated"], STOPPED_MID_RUN + [S.CHECKING_COMPLETION, S.COMPLETED]
    )
    for name in ("TailOne", "TailTwo"):
        workflow.store.assert_history_equals(tasks[name], SWEPT + RERUN_LADDER)
        assert workflow.target[tasks[name]] is True
