from winslow.task.status import TaskStatus as S

from harness import (
    COMPLETED_LADDER,
    DEP_BLOCKED_LADDER,
    FAILED_LADDER,
    build_workflow,
    by_params,
    run_all,
)

ODDS = (1, 3, 5, 7, 9)
EVENS = (2, 4, 6, 8, 10)


def test_depends_on_narrows_to_matching_instances(e2e_repo, mode):
    """A class-level dependency on a parameterized family resolves per
    instance: depends_on filters the candidates, so each consumer holds
    exactly its matching producer - while the default (always True) keeps
    the whole family."""
    workflow = build_workflow(e2e_repo, "my-depends", mode)
    tasks = by_params(workflow)
    producers = {n: tasks[("Producer", n)] for n in range(1, 11)}

    for name, numbers in (("OddConsumer", ODDS), ("EvenConsumer", EVENS)):
        for n in numbers:
            assert tasks[(name, n)].dependent_tasks == (producers[n],)
    # FanIn has no depends_on override - one task, all ten producers.
    assert set(tasks[("FanIn",)].dependent_tasks) == set(producers.values())


def test_failed_producer_blocks_only_matching_consumer(e2e_repo, mode):
    """The runtime consequence of the narrowed graph: failing one producer
    blocks exactly the consumers whose depends_on selected it. Sequential
    execution (--disable-concurrency) keeps the dependency re-check
    bookkeeping deterministic enough to pin exact ladders."""
    workflow = build_workflow(e2e_repo, "my-depends", mode, "--disable-concurrency")
    tasks = by_params(workflow)
    # Producer(3) runs but writes nothing (see Producer.run), so it fails its
    # post-run check like real work that silently produced no artifact.
    workflow.target[("poison", 3)] = True

    run_all(workflow)

    # One extra re-check pair, not one per dependent: the batch caches checked
    # dependencies, so the first blocked dependent re-checks Producer(3) and
    # every later dependent reuses that verdict.
    workflow.store.assert_history_equals(
        tasks[("Producer", 3)], FAILED_LADDER + [S.CHECKING_COMPLETION, S.FAILED]
    )

    # Only the consumers whose depends_on selected Producer(3) block on it.
    workflow.store.assert_history_equals(tasks[("OddConsumer", 3)], DEP_BLOCKED_LADDER)
    workflow.store.assert_history_equals(tasks[("FanIn",)], DEP_BLOCKED_LADDER)
    assert tasks[("OddConsumer", 3)] not in workflow.target
    assert tasks[("FanIn",)] not in workflow.target

    # Everyone else is untouched by the failure - the narrowed graph is what
    # keeps a 1-of-10 producer failure from fanning out to all consumers.
    unaffected = (
        [("Producer", n) for n in range(1, 11) if n != 3]
        + [("OddConsumer", n) for n in ODDS if n != 3]
        + [("EvenConsumer", n) for n in EVENS]
    )
    for key in unaffected:
        workflow.store.assert_history_equals(tasks[key], COMPLETED_LADDER)
        assert workflow.target[tasks[key]] is True
