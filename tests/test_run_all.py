import pytest

from winslow.constants import Mode

from harness import (
    BLOCKED_LADDER,
    COMPLETED_LADDER,
    FAILED_LADDER,
    CHECK_PASSED_LADDER,
    SKIPPED_LADDER,
    build_workflow,
    by_name,
    run_all,
)


def test_run_all_spectrum(workflow):
    """One full run covers the outcome spectrum: the chain completes and lands
    in the target, FailingCheck fails, Blocked blocks, Ineligible skips."""
    run_all(workflow)
    tasks = by_name(workflow)

    # The ladder proves the path taken; the target entry proves run() actually
    # did its work - statuses alone can't tell a real run from a no-op.
    for name in ("Alpha", "Beta", "Gamma"):
        workflow.store.assert_history_equals(tasks[name], COMPLETED_LADDER)
        assert workflow.target[tasks[name]] is True

    # FailingCheck's run() writes nothing, so its post-run check fails.
    workflow.store.assert_history_equals(tasks["FailingCheck"], FAILED_LADDER)
    # Blocked's can_run gate refuses - the ladder stops before RUNNING.
    workflow.store.assert_history_equals(tasks["Blocked"], BLOCKED_LADDER)
    # Ineligible fails the eligibility gate - no run path entered at all.
    workflow.store.assert_history_equals(tasks["Ineligible"], SKIPPED_LADDER)
    # Failure outcomes must leave no side effects - otherwise a rerun would
    # find phantom completed work and skip tasks that never really ran.
    for name in ("FailingCheck", "Blocked", "Ineligible"):
        assert tasks[name] not in workflow.target


def test_preseeded_target_completes_without_running(workflow):
    """A pre-seeded target simulates already-done work: the up-front success
    check passes and nothing runs - even Blocked, whose gate is never reached."""
    tasks = by_name(workflow)
    # Seeding the target is how "this work was already done in an earlier run"
    # looks to a task's success check.
    for name in ("Alpha", "Beta", "Gamma", "FailingCheck", "Blocked"):
        workflow.target[tasks[name]] = True

    run_all(workflow)

    # The up-front completion check passes for everyone, so nobody enters the
    # run path - Blocked included: its refusing can_run gate sits on the run
    # path and is never consulted. This is the idempotency contract.
    for name in ("Alpha", "Beta", "Gamma", "FailingCheck", "Blocked"):
        workflow.store.assert_history_equals(tasks[name], CHECK_PASSED_LADDER)
    # Eligibility precedes everything - a pre-seeded target can't resurrect
    # a skipped task.
    workflow.store.assert_history_equals(tasks["Ineligible"], SKIPPED_LADDER)


@pytest.mark.parametrize(
    "name",
    [
        "my-workflow",
        "my-params",
        "my-errors",
        "my-check-errors",
        "my-actions",
        "my-constraints",
    ],
)
def test_tandem_history_equivalence(e2e_repo, name):
    """The two runners must produce identical per-task transition sequences
    for the same scenario - the drift regression test."""
    histories = {}
    for mode in (Mode.HEADLESS, Mode.TUI):
        workflow = build_workflow(e2e_repo, name, mode)
        run_all(workflow)
        # Identity keys are parameter-based, so they match across the legs
        # even though the task instances differ.
        histories[mode] = dict(workflow.store.history)

    # Not just the same terminal statuses: the full transition sequences must
    # match. Any semantic drift between the runners (an extra status, a
    # reorder, a skipped step) fails here.
    assert histories[Mode.HEADLESS] == histories[Mode.TUI]
