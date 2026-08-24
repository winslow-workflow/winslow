"""Interactive-only: task claims exist on the interactive runner, where
multiple live batches can converge on one task (a per-task button clicked
while a bulk run is in flight). A clash parks the late batch on the claim -
release-notified, like the dependency wait - instead of refusing it."""

import time

from winslow.runner.execution import ExecutionStatus
from winslow.task.status import TaskStatus as S

from harness import COMPLETED_LADDER, gated_workflow, start_gated_batch


def wait_for_record_status(workflow, batch, task, status, timeout=5.0):
    """Poll the batch's execution record for a status. Record stores are born
    on the worker thread, so this also absorbs the store's own arrival."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        store = workflow.runner.record_store(batch.uuid)
        if store is not None and store.get(task.identity_key) is status:
            return store
        time.sleep(0.005)
    raise AssertionError(f"{task} never reached {status} in batch {batch.uuid[:8]}")


def test_busy_task_waits_for_release(e2e_repo):
    """A task mid-run in batch A is claimed; batch B single-running it parks
    on the claim (WAITING_FOR_RELEASE in B's record, batch-local) instead of
    executing. When A releases, B wakes, finds the work done, and confirms it
    - the main store carries no trace that B ever existed."""
    workflow, tasks = gated_workflow(e2e_repo, "--disable-concurrency")
    gate, batch_a = start_gated_batch(workflow, tasks)

    batch_b = workflow.runner.submit_run_single(tasks["Gated"])
    record_b = wait_for_record_status(
        workflow, batch_b, tasks["Gated"], S.WAITING_FOR_RELEASE
    )
    # Parked, not raced past: A still owns the task.
    assert workflow.store[tasks["Gated"]] is S.RUNNING

    gate.set()
    batch_a.wait()
    batch_b.wait()

    assert batch_a.status is ExecutionStatus.FINISHED
    assert batch_b.status is ExecutionStatus.FINISHED
    # B's verdict is the passing-status shortcut's mirror: waited, then
    # confirmed done - no second run.
    assert record_b[tasks["Gated"].identity_key] is S.COMPLETED
    for name in ("Gated", "TailOne", "TailTwo"):
        workflow.store.assert_history_equals(tasks[name], COMPLETED_LADDER)


def test_stop_while_waiting_aborts_batch_locally(e2e_repo):
    """Stopping the parked batch ends its wait: B lands STOPPED with ABORTED
    in its own record, and the owning batch never notices."""
    workflow, tasks = gated_workflow(e2e_repo, "--disable-concurrency")
    # Stops don't notify the claim condition; shrink the poll slice so the
    # test doesn't ride the production 0.5s.
    workflow.runner.CLAIM_STOP_POLL_SECONDS = 0.01
    gate, batch_a = start_gated_batch(workflow, tasks)

    batch_b = workflow.runner.submit_run_single(tasks["Gated"])
    record_b = wait_for_record_status(
        workflow, batch_b, tasks["Gated"], S.WAITING_FOR_RELEASE
    )

    batch_b.request_stop()
    batch_b.wait()

    assert batch_b.status is ExecutionStatus.STOPPED
    assert record_b[tasks["Gated"].identity_key] is S.ABORTED

    gate.set()
    batch_a.wait()

    assert batch_a.status is ExecutionStatus.FINISHED
    for name in ("Gated", "TailOne", "TailTwo"):
        workflow.store.assert_history_equals(tasks[name], COMPLETED_LADDER)
