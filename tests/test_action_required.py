from winslow.task.status import TaskStatus as S

from harness import (
    DEP_BLOCKED_LADDER,
    PRE_RUN,
    RERUN_LADDER,
    RUN_AND_CHECK,
    build_workflow,
    by_name,
    check_batch,
    run_all,
    run_batch,
)

# The run succeeded but the check demands a human sign-off: neither COMPLETED
# nor honestly FAILED, so the task pauses. The trailing pair is the
# dependent's dependency resolution asking again - being paused is not an
# answer, so the signal re-raises.
PAUSED_LADDER = (
    PRE_RUN
    + RUN_AND_CHECK
    + [S.ACTION_REQUIRED, S.CHECKING_COMPLETION, S.ACTION_REQUIRED]
)


def test_action_required_pauses_until_signed_off(e2e_repo, mode):
    """The human-in-the-loop flow: work lands, the task pauses on a missing
    sign-off, and a later check - not a re-run - resumes it once the human
    (here: the test) has acted."""
    workflow = build_workflow(e2e_repo, "my-actions", mode)
    tasks = by_name(workflow)

    run_all(workflow)

    # Paused after really working: the work marker is the evidence the pause
    # came from the sign-off gate, not from a failed run.
    workflow.store.assert_history_equals(tasks["NeedsApproval"], PAUSED_LADDER)
    assert workflow.target[tasks["NeedsApproval"]] is True
    # A paused dependency is an unsatisfied dependency.
    workflow.store.assert_history_equals(tasks["DependsOnApproval"], DEP_BLOCKED_LADDER)

    workflow.target[("approved", tasks["NeedsApproval"])] = True
    check_batch(workflow)

    # One check pair and done - resuming needs no re-run, because the check
    # reads the world and the world now contains the sign-off.
    workflow.store.assert_history_equals(
        tasks["NeedsApproval"], PAUSED_LADDER + [S.CHECKING_COMPLETION, S.COMPLETED]
    )

    run_batch(workflow)

    # The dependent, unlike its dependency, still owes a run - the check
    # batch re-failed it honestly, the run batch now completes it.
    workflow.store.assert_history_equals(
        tasks["DependsOnApproval"],
        DEP_BLOCKED_LADDER + [S.CHECKING_COMPLETION, S.FAILED] + RERUN_LADDER,
    )
    assert workflow.target[tasks["DependsOnApproval"]] is True


def test_rerun_cannot_bypass_the_pause(e2e_repo, mode):
    """Re-running is not a substitute for the sign-off: the up-front check
    re-raises the pause, and the runner refuses to run anything not verified
    FAILED - so the gate holds and no work is redone."""
    workflow = build_workflow(e2e_repo, "my-actions", mode)
    tasks = by_name(workflow)
    run_all(workflow)

    run_batch(workflow)

    # No second RUNNING anywhere in the ladder: one pause pair from the task's
    # own up-front check, one from the dependent's dependency resolution.
    workflow.store.assert_history_equals(
        tasks["NeedsApproval"],
        PAUSED_LADDER + [S.CHECKING_COMPLETION, S.ACTION_REQUIRED] * 2,
    )
    workflow.store.assert_history_equals(
        tasks["DependsOnApproval"],
        DEP_BLOCKED_LADDER
        + [S.CHECKING_COMPLETION, S.FAILED, S.CHECKING_DEPENDENCIES, S.BLOCKED],
    )
    # Exactly one run's worth of world - nothing ran twice, nothing new
    # appeared while the gate was closed.
    assert workflow.target == {tasks["NeedsApproval"]: True}
