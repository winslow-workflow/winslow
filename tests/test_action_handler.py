"""The action handler: the one inbound path of a session. An action resolves,
gates, delegates and answers with an ack; the outcomes arrive as bus events.
Interactive mode, like the lifecycle tests: the handler serves the TUI and the
future remote transports, and headless mode drives the runner itself."""

from winslow.actions import (
    Ack,
    BatchAck,
    CheckTasks,
    EndSession,
    RunTasks,
    SetBatchOptions,
    StopBatch,
)
from winslow.constants import Mode
from winslow.events import BatchCreatedEvent
from winslow.runner.execution import ExecutionStatus
from winslow.task.status import TaskStatus as S

from harness import build_workflow, by_name, gated_workflow, ready, start_gated_batch


def live_workflow(e2e_repo):
    return ready(build_workflow(e2e_repo, "my-workflow", Mode.TUI))


def submit_and_wait(workflow, action):
    ack = workflow.session.actions.submit(action)
    if ack.accepted:
        workflow.runner.execution_batches_map[ack.batch_uuid].wait()
    return ack


def test_run_tasks_single_key_creates_a_batch(e2e_repo):
    workflow = live_workflow(e2e_repo)
    alpha = by_name(workflow)["Alpha"]

    ack = submit_and_wait(workflow, RunTasks(keys=(alpha.identity_key,)))

    assert ack == BatchAck(accepted=True, batch_uuid=ack.batch_uuid)
    assert ack.batch_uuid in workflow.runner.execution_batches_map
    assert workflow.store[alpha] is S.COMPLETED


def test_run_tasks_many_keys_creates_one_batch(e2e_repo):
    workflow = live_workflow(e2e_repo)
    keys = tuple(task.identity_key for task in workflow.tasks)

    ack = submit_and_wait(workflow, RunTasks(keys=keys))

    assert ack.accepted
    batch = workflow.runner.execution_batches_map[ack.batch_uuid]
    # The admission filtered the ineligible task out of the roster.
    assert batch.task_count == len(keys) - 1


def test_check_tasks_creates_a_check_batch(e2e_repo):
    workflow = live_workflow(e2e_repo)
    alpha = by_name(workflow)["Alpha"]

    ack = submit_and_wait(workflow, CheckTasks(keys=(alpha.identity_key,)))

    assert ack.accepted
    assert workflow.store[alpha] is S.FAILED  # checked, never run


def test_an_unknown_key_refuses_with_the_key_in_the_reason(e2e_repo):
    workflow = live_workflow(e2e_repo)

    ack = workflow.session.actions.submit(RunTasks(keys=("ghost-00000000",)))

    assert ack == BatchAck(accepted=False, reason=ack.reason)
    assert "ghost-00000000" in ack.reason


def test_only_ineligible_keys_refuse(e2e_repo):
    workflow = live_workflow(e2e_repo)
    ineligible = by_name(workflow)["Ineligible"]

    ack = workflow.session.actions.submit(RunTasks(keys=(ineligible.identity_key,)))

    assert not ack.accepted
    assert "eligible" in ack.reason


def test_every_action_refuses_after_the_session_ends(e2e_repo):
    workflow = live_workflow(e2e_repo)
    actions = workflow.session.actions

    assert actions.submit(EndSession()) == Ack(accepted=True)
    assert workflow.session.has_ended

    refused = [
        actions.submit(RunTasks(keys=("k",))),
        actions.submit(CheckTasks(keys=("k",))),
        actions.submit(StopBatch(batch_uuid="b")),
        actions.submit(EndSession()),
        actions.submit(SetBatchOptions(dry_run=True)),
    ]
    for ack in refused:
        assert not ack.accepted
        assert workflow.session.session_id in ack.reason
    # A batch action refuses with the batch-shaped ack.
    assert isinstance(refused[0], BatchAck)


def test_set_batch_options_changes_one_field_and_the_manifest(e2e_repo, state_store):
    workflow = live_workflow(e2e_repo)
    workflow.init_state(state_store, origin="test")

    ack = workflow.session.actions.submit(SetBatchOptions(dry_run=True))

    assert ack == Ack(accepted=True)
    assert workflow.batch_options.dry_run is True
    assert workflow.batch_options.force_run is False  # None fields stay
    manifest = state_store.load_manifest(workflow.session_id)
    assert manifest.orchestrator_overrides["dry_run"] is True


def test_stop_batch_acknowledges_and_the_batch_drains(e2e_repo):
    workflow, tasks = gated_workflow(e2e_repo, "--disable-concurrency")
    gate, batch = start_gated_batch(workflow, tasks)

    ack = workflow.session.actions.submit(StopBatch(batch_uuid=batch.uuid))

    # The acceptance means "stop requested": the batch is still draining.
    assert ack == Ack(accepted=True)
    gate.set()
    batch.wait()
    assert batch.status is ExecutionStatus.STOPPED
    for name in ("TailOne", "TailTwo"):
        assert workflow.store[tasks[name]] is S.ABORTED


def test_stop_batch_refuses_an_unknown_uuid(e2e_repo):
    workflow = live_workflow(e2e_repo)

    ack = workflow.session.actions.submit(StopBatch(batch_uuid="no-such-batch"))

    assert not ack.accepted
    assert "no-such-batch" in ack.reason


def test_an_accepted_action_arrives_as_bus_events(e2e_repo):
    workflow = live_workflow(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    created = []
    workflow.bus.subscribe(BatchCreatedEvent, lambda event: created.append(event))

    ack = submit_and_wait(workflow, RunTasks(keys=(alpha.identity_key,)))

    assert [event.batch.uuid for event in created] == [ack.batch_uuid]
