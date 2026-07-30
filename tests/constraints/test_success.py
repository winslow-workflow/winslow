from harness import COMPLETED_LADDER, FAILED_LADDER, by_name, run_all


def test_failing_success_constraint_fails(constraints_workflow):
    """A failing success constraint vetoes an otherwise-passing check: the task
    really runs, yet the constraint-AND-check verdict is False, so it fails."""
    workflow = constraints_workflow
    run_all(workflow)
    tasks = by_name(workflow)

    workflow.store.assert_history_equals(tasks["ConstraintFailsCheck"], FAILED_LADDER)


def test_success_constraint_alone_defines_success(constraints_workflow):
    """No check() override at all: the success constraint alone decides
    completion, and completes once run() has written the marker."""
    workflow = constraints_workflow
    run_all(workflow)
    tasks = by_name(workflow)

    workflow.store.assert_history_equals(tasks["SuccessByConstraint"], COMPLETED_LADDER)
    assert workflow.target[tasks["SuccessByConstraint"]] is True
