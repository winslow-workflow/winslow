from harness import BLOCKED_LADDER, COMPLETED_LADDER, by_name, run_all


def test_passing_runnability_constraint_runs(constraints_workflow):
    """A satisfied runnability constraint lets the task through its runnability
    gate and on to completion."""
    workflow = constraints_workflow
    run_all(workflow)
    tasks = by_name(workflow)

    workflow.store.assert_history_equals(tasks["Completes"], COMPLETED_LADDER)
    assert workflow.target[tasks["Completes"]] is True


def test_failing_runnability_constraint_blocks(constraints_workflow):
    """A failing runnability constraint blocks at the runnability gate - the
    constraint form of can_run() -> False. The ladder stops before RUNNING."""
    workflow = constraints_workflow
    run_all(workflow)
    tasks = by_name(workflow)

    workflow.store.assert_history_equals(tasks["ConstraintBlocks"], BLOCKED_LADDER)
    assert tasks["ConstraintBlocks"] not in workflow.target
