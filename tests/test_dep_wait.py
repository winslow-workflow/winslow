"""The concurrent dependency wait (_resolve_dependencies' backoff loop): a
consumer whose dependency is ACTIVE in another batch parks until the dep
settles. Cross-batch by construction, so both legs drive the submit API
directly instead of run_all."""

import threading
import time

from winslow.task.status import TaskStatus as S

from harness import (
    COMPLETED_LADDER,
    DEP_BLOCKED_LADDER,
    FAILED_LADDER,
    build_workflow,
    by_name,
    ready,
    wait_for_status,
)


def slow_deps_workflow(e2e_repo, mode):
    # No timing patch: the settle wakes the waiter through the store
    # condition, so these tests are fast only while that stays true - suite
    # duration is the regression signal for a broken wake-up.
    workflow = ready(build_workflow(e2e_repo, "my-slow-deps", mode))
    return workflow, by_name(workflow)


def parked_consumer(workflow, tasks, producer_name, consumer_name):
    """Producer mid-run in batch A, consumer submitted as batch B and parked
    on the dependency wait; returns the gate and both batch handles."""
    producer, consumer = tasks[producer_name], tasks[consumer_name]
    gate = threading.Event()
    workflow.target[("gate", producer)] = gate

    batch_a = workflow.runner.submit_run([producer])
    wait_for_status(workflow, producer, S.RUNNING)
    batch_b = workflow.runner.submit_run([consumer])

    # The park has no status of its own - the wait runs in the group pre-pass,
    # before the consumer's own ladder starts - so prove it by elapsed time:
    # long after a broken wait would have finished, B is still open and the
    # consumer untouched.
    time.sleep(0.15)
    assert batch_b.completed_at is None
    assert workflow.store[consumer] is S.READY_TO_PROCESS

    return gate, batch_a, batch_b


def test_dep_wait_releases_on_success(e2e_repo, mode):
    """The consumer waits out its ACTIVE dependency and proceeds once it
    settles COMPLETED. The settle is trusted as-is - no re-check pair on the
    producer's ladder - and both tasks end on the plain completed ladder."""
    workflow, tasks = slow_deps_workflow(e2e_repo, mode)
    gate, batch_a, batch_b = parked_consumer(
        workflow, tasks, "SlowProducer", "NeedsProducer"
    )

    gate.set()
    batch_a.wait()
    batch_b.wait()

    workflow.store.assert_history_equals(tasks["SlowProducer"], COMPLETED_LADDER)
    workflow.store.assert_history_equals(tasks["NeedsProducer"], COMPLETED_LADDER)
    assert workflow.target[tasks["NeedsProducer"]] is True


def test_dep_wait_blocks_on_failure(e2e_repo, mode):
    """The same wait, settling on a failed dependency: the consumer must not
    run blind - it blocks, exactly as it would on a same-batch failure."""
    workflow, tasks = slow_deps_workflow(e2e_repo, mode)
    gate, batch_a, batch_b = parked_consumer(
        workflow, tasks, "SlowFailer", "NeedsFailer"
    )

    gate.set()
    batch_a.wait()
    batch_b.wait()

    # The trailing pair is the waiting batch giving the settled-failed dep
    # its second-chance re-check before giving up on it.
    workflow.store.assert_history_equals(
        tasks["SlowFailer"], FAILED_LADDER + [S.CHECKING_COMPLETION, S.FAILED]
    )
    workflow.store.assert_history_equals(tasks["NeedsFailer"], DEP_BLOCKED_LADDER)
    assert tasks["NeedsFailer"] not in workflow.target
