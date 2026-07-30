from harness import (
    CHECK_FAILED_LADDER,
    CHECK_PASSED_LADDER,
    FORCE_SUCCESS_LADDER,
    SKIPPED_LADDER,
    build_workflow,
    by_name,
    check_all,
)

SEEDED = ("Alpha", "Beta", "Gamma", "Blocked")


def test_check_only(e2e_repo, mode):
    """Pure check: checks observe the target, they never repair it. Seeded
    work verifies as COMPLETED_PREVIOUSLY (nothing ever ran); missing work
    stays FAILED because there is no run step to fall through to."""
    workflow = build_workflow(e2e_repo, "my-workflow", mode, "--check")
    tasks = by_name(workflow)
    for name in SEEDED:
        workflow.target[tasks[name]] = True

    check_all(workflow)

    # Blocked is seeded and passes too: its refusing can_run gate guards the
    # run path, which a pure check never enters.
    for name in SEEDED:
        workflow.store.assert_history_equals(tasks[name], CHECK_PASSED_LADDER)
    # Unseeded work cannot be fixed by checking - and no run may be attempted.
    workflow.store.assert_history_equals(tasks["FailingCheck"], CHECK_FAILED_LADDER)
    workflow.store.assert_history_equals(tasks["Ineligible"], SKIPPED_LADDER)
    # Checks must be read-only against the target - a check that writes would
    # make the next check pass vacuously.
    assert tasks["FailingCheck"] not in workflow.target


def test_force_success_bypasses_checks(e2e_repo, mode):
    """Same seeded target as test_check_only, opposite mechanism: force_success
    is an operator override, so it must not consult the checks at all - every
    checkable task lands FORCE_SUCCESS (not COMPLETED), seeded or not."""
    workflow = build_workflow(
        e2e_repo, "my-workflow", mode, "--check", "--force-success"
    )
    tasks = by_name(workflow)
    for name in SEEDED:
        workflow.target[tasks[name]] = True

    check_all(workflow)

    # The unseeded FailingCheck passes exactly like the seeded tasks: proof
    # the target (and therefore check()) was never consulted. The distinct
    # FORCE_SUCCESS status keeps the override auditable next to real passes.
    for name in (*SEEDED, "FailingCheck"):
        workflow.store.assert_history_equals(tasks[name], FORCE_SUCCESS_LADDER)
    # Eligibility still gates first - force_success can't resurrect a skip.
    workflow.store.assert_history_equals(tasks["Ineligible"], SKIPPED_LADDER)
    # Forcing success records no work as done - the target is evidence of real
    # runs only.
    assert tasks["FailingCheck"] not in workflow.target
