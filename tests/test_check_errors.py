import pytest

from winslow.exceptions import IllegalTaskOutcomeError, TaskSkip
from winslow.task.status import TaskStatus as S

from harness import (
    COMPLETED_LADDER,
    DEP_BLOCKED_LADDER,
    PRE_RUN,
    RUN_AND_CHECK,
    UNPROCESSED,
    build_workflow,
    by_name,
    check_batch,
    run_all,
    run_batch,
)

# A check that raises before the run path is entered: with no trustworthy
# answer to "am I already done?", the run must not happen at all.
ERROR_PRE_RUN_CHECK = UNPROCESSED + [S.CHECKING_COMPLETION, S.ERROR]

# The post-run counterpart: the run landed its work, then the verifying check
# raised - RUN_FINISHED is on the ladder, but COMPLETED never arrives.
ERROR_POST_RUN_CHECK = PRE_RUN + RUN_AND_CHECK + [S.ERROR]


def test_check_error_is_contained(e2e_repo, mode):
    """A raising check is a defect (ERROR), not a verdict (FAILED), in
    whichever phase it fires - and like any defect it is contained: the
    dependent blocks, the rest of the batch proceeds."""
    workflow = build_workflow(e2e_repo, "my-check-errors", mode)
    tasks = by_name(workflow)

    run_all(workflow)

    # The trailing pair is DependsOnBoomCheck's dependency resolution giving
    # BoomCheck its second chance - whose check just raises again.
    workflow.store.assert_history_equals(
        tasks["BoomCheck"], ERROR_PRE_RUN_CHECK + [S.CHECKING_COMPLETION, S.ERROR]
    )
    workflow.store.assert_history_equals(
        tasks["DependsOnBoomCheck"], DEP_BLOCKED_LADDER
    )
    workflow.store.assert_history_equals(
        tasks["ChokesOnArtifact"], ERROR_POST_RUN_CHECK
    )
    workflow.store.assert_history_equals(tasks["Innocent"], COMPLETED_LADDER)

    # BoomCheck died before its run - no phantom work. ChokesOnArtifact's run
    # landed its artifact before the check died: real work under an ERROR
    # status, which is exactly what a later pass must not silently re-label.
    assert workflow.target == {
        tasks[name]: True for name in ("ChokesOnArtifact", "Innocent")
    }


def test_reraise_errors_aborts_on_check_error(e2e_repo, mode):
    """--reraise-errors treats a check defect exactly like a run defect:
    mark ERROR first, then let the original exception propagate and abort
    the batch. Sequential execution makes "everything after" deterministic
    - BoomCheck sorts first, so nothing else is ever processed."""
    workflow = build_workflow(
        e2e_repo, "my-check-errors", mode, "--reraise-errors", "--disable-concurrency"
    )
    tasks = by_name(workflow)

    with pytest.raises(RuntimeError, match="boom check"):
        run_all(workflow)

    workflow.store.assert_history_equals(tasks["BoomCheck"], ERROR_PRE_RUN_CHECK)
    for name in ("ChokesOnArtifact", "DependsOnBoomCheck", "Innocent", "SkipsMidCheck"):
        workflow.store.assert_history_equals(tasks[name], UNPROCESSED)
    assert workflow.target == {}


def test_illegal_signal_is_a_defect(e2e_repo, mode):
    """A signal raised where no ladder consumes it (skip is eligibility's
    verb, fired here mid-check) is a defect like any raise: ERROR, never
    SKIPPED - honoring it as flow control would skip a task eligibility
    never cleared."""
    workflow = build_workflow(e2e_repo, "my-check-errors", mode)
    tasks = by_name(workflow)

    run_all(workflow)

    workflow.store.assert_history_equals(tasks["SkipsMidCheck"], ERROR_PRE_RUN_CHECK)
    assert tasks["SkipsMidCheck"] not in workflow.target


def test_reraise_swaps_illegal_signal_for_typed_error(e2e_repo, mode):
    """The reraise leg must not propagate the signal itself - an upstream
    ladder would consume it as legal flow control. What surfaces is
    IllegalTaskOutcomeError naming the signal and the phase it abused,
    with the signal as its cause and the task marked ERROR first."""
    workflow = build_workflow(
        e2e_repo, "my-check-errors", mode, "--reraise-errors", "--disable-concurrency"
    )
    tasks = by_name(workflow)
    # The full run aborts on BoomCheck (pinned above) - it doubles here as
    # the eligibility pre-pass; the illegal signal needs its own batch.
    with pytest.raises(RuntimeError, match="boom check"):
        run_all(workflow)

    with pytest.raises(
        IllegalTaskOutcomeError,
        match="TaskSkip is not a legal signal during pre_run_check",
    ) as excinfo:
        run_batch(workflow, [tasks["SkipsMidCheck"]])

    assert isinstance(excinfo.value.__cause__, TaskSkip)
    workflow.store.assert_history_equals(tasks["SkipsMidCheck"], ERROR_PRE_RUN_CHECK)


def test_calmed_check_lands_completed_with_error(e2e_repo, mode):
    """The errored seed keys off ERROR regardless of which phase raised: a
    repaired (calmed) check that now passes over the errored attempt's real
    work lands COMPLETED_WITH_ERROR, not COMPLETED - the same no-laundering
    rule the run-side flag follows."""
    workflow = build_workflow(e2e_repo, "my-check-errors", mode)
    tasks = by_name(workflow)
    run_all(workflow)

    workflow.target[("calm", tasks["ChokesOnArtifact"])] = True
    check_batch(workflow)

    workflow.store.assert_history_equals(
        tasks["ChokesOnArtifact"],
        ERROR_POST_RUN_CHECK + [S.CHECKING_COMPLETION, S.COMPLETED_WITH_ERROR],
    )
    # The unrepaired check restates its defect...
    workflow.store.assert_history_equals(
        tasks["BoomCheck"], ERROR_PRE_RUN_CHECK + [S.CHECKING_COMPLETION, S.ERROR] * 2
    )
    # ...while the clean neighbor re-checks to a clean COMPLETED.
    workflow.store.assert_history_equals(
        tasks["Innocent"], COMPLETED_LADDER + [S.CHECKING_COMPLETION, S.COMPLETED]
    )
