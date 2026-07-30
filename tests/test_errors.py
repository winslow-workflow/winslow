import pytest

from winslow.task.status import TaskStatus as S

from harness import (
    COMPLETED_LADDER,
    DEP_BLOCKED_LADDER,
    PRE_RUN,
    UNPROCESSED,
    build_workflow,
    by_name,
    check_batch,
    run_all,
)

# An uncaught exception in run(): no RUN_FINISHED (the raise aborted before
# it), and no post-run check - completion of code that just blew up is
# meaningless, so the runner returns early on ERROR (see process_task).
ERROR_RUN = PRE_RUN + [S.READY_TO_RUN, S.RUNNING, S.ERROR]


def test_error_is_contained_by_default(e2e_repo, mode):
    """ERROR means "the task's code broke", as opposed to FAILED ("the check
    honestly said not done"). By default the defect is contained: it blocks
    its dependents like any unsatisfied dependency, and the rest of the batch
    proceeds untouched."""
    workflow = build_workflow(e2e_repo, "my-errors", mode)
    tasks = by_name(workflow)

    run_all(workflow)

    # The trailing pair is DependsOnBoom's dependency resolution re-checking
    # Boom (the same second chance every failed dependency gets) - which
    # honestly finds nothing and relabels the defect a plain FAILED.
    workflow.store.assert_history_equals(
        tasks["Boom"], ERROR_RUN + [S.CHECKING_COMPLETION, S.FAILED]
    )
    # ERROR is an unsatisfied dependency like any other - dependents block on
    # a defect exactly as they do on an honest failure.
    workflow.store.assert_history_equals(tasks["DependsOnBoom"], DEP_BLOCKED_LADDER)
    workflow.store.assert_history_equals(tasks["Innocent"], COMPLETED_LADDER)

    # PartialBoom did its work before raising, so the re-check honestly finds
    # the artifact - but a passing check doesn't launder the defect: the flag
    # rides the status, and the dependent (satisfied by any passing status)
    # proceeds normally.
    workflow.store.assert_history_equals(
        tasks["PartialBoom"],
        ERROR_RUN + [S.CHECKING_COMPLETION, S.COMPLETED_WITH_ERROR],
    )
    workflow.store.assert_history_equals(tasks["DependsOnPartial"], COMPLETED_LADDER)

    # Boom's raise happened before its target write - a defect leaves no
    # phantom work behind; PartialBoom's landed, which is the whole problem.
    assert workflow.target == {
        tasks[name]: True for name in ("Innocent", "PartialBoom", "DependsOnPartial")
    }


def test_reraise_errors_aborts_the_run(e2e_repo, mode):
    """--reraise-errors is the CI/debugging mode: the task is still marked
    ERROR first, then the original exception propagates out of the batch,
    aborting everything after it. Sequential execution makes "after it"
    deterministic."""
    workflow = build_workflow(
        e2e_repo, "my-errors", mode, "--reraise-errors", "--disable-concurrency"
    )
    tasks = by_name(workflow)

    with pytest.raises(RuntimeError, match="boom"):
        run_all(workflow)

    # Marked before re-raising, and terminal: the abort means no dependent
    # ever re-checks Boom, so no trailing CHECKING_COMPLETION > FAILED pair.
    workflow.store.assert_history_equals(tasks["Boom"], ERROR_RUN)
    # The rest of the batch never processes - contrast with the default mode,
    # where Innocent completes and DependsOnBoom gets a real BLOCKED verdict.
    for name in ("DependsOnBoom", "Innocent", "PartialBoom", "DependsOnPartial"):
        workflow.store.assert_history_equals(tasks[name], UNPROCESSED)
    assert workflow.target == {}


def test_completed_with_error_survives_recheck(e2e_repo, mode):
    """Looking again doesn't launder the defect: a later check-only batch
    seeds its errored set from the durable statuses, re-verifies the artifact,
    and restates COMPLETED_WITH_ERROR. Only a fresh run attempt can earn a
    clean COMPLETED."""
    workflow = build_workflow(e2e_repo, "my-errors", mode)
    tasks = by_name(workflow)
    run_all(workflow)

    check_batch(workflow)

    workflow.store.assert_history_equals(
        tasks["PartialBoom"],
        ERROR_RUN + [S.CHECKING_COMPLETION, S.COMPLETED_WITH_ERROR] * 2,
    )
    # The clean neighbor re-checks to a clean COMPLETED - the flag is
    # per-artifact, not a batch-wide stain.
    workflow.store.assert_history_equals(
        tasks["Innocent"], COMPLETED_LADDER + [S.CHECKING_COMPLETION, S.COMPLETED]
    )
