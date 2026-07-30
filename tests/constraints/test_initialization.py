from harness import COMPLETED_LADDER, by_name, run_all


def test_passing_initialization_constraint_present(constraints_workflow):
    """A passing initialization constraint keeps the task in the graph - it
    becomes an instance and runs normally."""
    workflow = constraints_workflow
    run_all(workflow)
    tasks = by_name(workflow)

    assert "Initializes" in tasks
    workflow.store.assert_history_equals(tasks["Initializes"], COMPLETED_LADDER)


def test_failing_initialization_constraint_absent(constraints_workflow):
    """A failing initialization constraint drops the task at graph-build - it
    never becomes an instance, so it's absent from the workflow entirely."""
    workflow = constraints_workflow
    tasks = by_name(workflow)

    assert "NeverInitialized" not in tasks
