from harness import COMPLETED_LADDER, SKIPPED_LADDER, by_name, run_all


def test_passing_eligibility_constraint_runs(constraints_workflow):
    """A satisfied eligibility constraint doesn't interfere - the task runs
    and completes."""
    workflow = constraints_workflow
    run_all(workflow)
    tasks = by_name(workflow)

    workflow.store.assert_history_equals(tasks["Completes"], COMPLETED_LADDER)
    assert workflow.target[tasks["Completes"]] is True


def test_failing_eligibility_constraint_skips(constraints_workflow):
    """A failing eligibility constraint skips the task before any run
    machinery - the constraint form of is_eligible() -> False."""
    workflow = constraints_workflow
    run_all(workflow)
    tasks = by_name(workflow)

    workflow.store.assert_history_equals(tasks["ConstraintSkips"], SKIPPED_LADDER)
    assert tasks["ConstraintSkips"] not in workflow.target


def test_eligibility_constraint_and_override_both_consulted(constraints_workflow):
    """Constraint passes but the is_eligible() override fails: eligibility is
    the AND of both, so the task still skips."""
    workflow = constraints_workflow
    run_all(workflow)
    tasks = by_name(workflow)

    workflow.store.assert_history_equals(tasks["OverrideSkips"], SKIPPED_LADDER)
    assert tasks["OverrideSkips"] not in workflow.target
