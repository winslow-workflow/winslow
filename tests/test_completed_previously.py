from winslow.task.status import TaskStatus as S

from harness import (
    CHECK_PASSED_LADDER,
    COMPLETED_LADDER,
    UNPROCESSED,
    by_name,
    check_batch,
    run_all,
)


def test_preseeded_task_lands_completed_previously(workflow):
    """A pre-seeded non-noop task passes its up-front check without a run:
    this workflow never ran it, so it lands COMPLETED_PREVIOUSLY - the honest
    version of COMPLETED for work that predates the workflow."""
    tasks = by_name(workflow)
    workflow.target[tasks["Alpha"]] = True

    run_all(workflow)

    workflow.store.assert_history_equals(tasks["Alpha"], CHECK_PASSED_LADDER)
    assert workflow.store[tasks["Alpha"]] is S.COMPLETED_PREVIOUSLY


def test_recheck_after_run_keeps_completed(workflow):
    """The demotion guard: a task that really ran stays COMPLETED when a later
    check batch re-probes it. The check phase alone can't tell a first probe
    from a re-probe - _has_been_run on the task instance can."""
    tasks = by_name(workflow)
    run_all(workflow)
    workflow.store.assert_history_equals(tasks["Alpha"], COMPLETED_LADDER)

    check_batch(workflow, [tasks["Alpha"]])

    workflow.store.assert_history_equals(
        tasks["Alpha"], COMPLETED_LADDER + [S.CHECKING_COMPLETION, S.COMPLETED]
    )


def test_noop_check_lands_completed(workflow):
    """A noop task has no run action, so "previously" does not apply: its
    passing check lands plain COMPLETED, seeded or not."""
    tasks = by_name(workflow)
    workflow.target[tasks["Noop"]] = True

    run_all(workflow)

    workflow.store.assert_history_equals(
        tasks["Noop"], UNPROCESSED + [S.CHECKING_COMPLETION, S.COMPLETED]
    )
