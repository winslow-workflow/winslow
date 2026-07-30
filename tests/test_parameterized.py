from harness import COMPLETED_LADDER, CHECK_PASSED_LADDER, by_params, run_all

DEPLOYS = [("Deploy", region) for region in ("ap", "eu", "us")]
MATRIX = [("Matrix", "eu", "free"), ("Matrix", "us", "paid")]


def test_run_all_parameterized(params_workflow):
    """Every instance of a parameterized class is its own task: Deploy fans out
    per region, Matrix per compound row, and Collect waits on the whole Deploy
    family. Each instance runs the full ladder and lands its own target entry."""
    run_all(params_workflow)
    tasks = by_params(params_workflow)

    # Asserted before any status checks so a wrong fan-out fails on the count,
    # not as a confusing KeyError deeper in.
    assert set(tasks) == {*DEPLOYS, *MATRIX, ("Collect",)}
    # Every instance is an independent task: its own ladder, its own target
    # entry - nothing shared through the class.
    for task in tasks.values():
        params_workflow.store.assert_history_equals(task, COMPLETED_LADDER)
        assert params_workflow.target[task] is True


def test_preseeded_instance_is_independent(params_workflow):
    """Pre-seeding one Deploy instance short-circuits only that instance - its
    siblings (same class, different parameters) still run the full ladder."""
    tasks = by_params(params_workflow)
    params_workflow.target[tasks[("Deploy", "eu")]] = True

    run_all(params_workflow)

    # The pre-seeded instance passes its up-front check and never runs...
    params_workflow.store.assert_history_equals(
        tasks[("Deploy", "eu")], CHECK_PASSED_LADDER
    )
    # ...while its same-class siblings run fully: parameters, not classes,
    # define task identity.
    for key in (("Deploy", "ap"), ("Deploy", "us")):
        params_workflow.store.assert_history_equals(tasks[key], COMPLETED_LADDER)
    # Collect's dependency on the Deploy family is satisfied by pre-seeded and
    # freshly-run instances alike.
    params_workflow.store.assert_history_equals(tasks[("Collect",)], COMPLETED_LADDER)
