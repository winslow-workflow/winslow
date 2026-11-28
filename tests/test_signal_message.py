"""The message of a task signal reaches a client as data: the history read
serves it on the TaskOutcome of the task, over the local adapter, the
websocket and the MCP endpoint. An agent reads the prompt of a paused task
without a log read."""

from dataclasses import asdict

from winslow.client.local import LocalSessionClient
from winslow.client.websocket import RemoteAppClient
from winslow.runner import ExecutionRecordStore
from winslow.task.status import TaskStatus as S

from harness import TOKEN, by_name
from test_client_local import run_to_completion
from test_client_websocket import ServedProcess, registered
from test_serve_mcp import call_tools, mcp_app

APPROVAL_PROMPT = "approval marker missing - sign off to proceed"


def paused_session(e2e_repo):
    """A session of my-actions with NeedsApproval run into its pause."""
    workflow, session, registry = registered(e2e_repo, "my-actions")
    client = LocalSessionClient(session)
    task = by_name(workflow)["NeedsApproval"]
    ack = run_to_completion(workflow, client, task)
    return workflow, session, registry, task, ack


def test_the_history_carries_the_prompt_of_a_paused_task(e2e_repo):
    workflow, session, registry, task, ack = paused_session(e2e_repo)

    (row,) = LocalSessionClient(session).history()
    outcome = row.tasks[task.identity_key]

    assert row.uuid == ack.batch_uuid
    assert outcome.status == "ACTION_REQUIRED"
    assert outcome.signal_message == APPROVAL_PROMPT


def test_a_check_that_passes_after_the_sign_off_carries_no_message(e2e_repo):
    workflow, session, registry, task, _ = paused_session(e2e_repo)
    client = LocalSessionClient(session)

    workflow.target[("approved", task)] = True
    ack = client.check_tasks(keys=(task.identity_key,))
    workflow.runner.get_batch(ack.batch_uuid).wait()

    paused, signed_off = client.history()
    # The record of the first batch is history: it keeps the prompt it showed.
    assert paused.tasks[task.identity_key].signal_message == APPROVAL_PROMPT
    assert signed_off.tasks[task.identity_key].status == "COMPLETED"
    assert signed_off.tasks[task.identity_key].signal_message == ""


def test_a_task_behind_a_paused_dependency_names_its_block(e2e_repo):
    workflow, session, registry, needs, _ = paused_session(e2e_repo)
    client = LocalSessionClient(session)
    dependent = by_name(workflow)["DependsOnApproval"]

    run_to_completion(workflow, client, dependent)

    _, row = client.history()
    # The dependency is outside this batch and has no record in it. The block
    # message names it, so a client knows which task to check.
    assert set(row.tasks) == {dependent.identity_key}
    outcome = row.tasks[dependent.identity_key]
    # The handler above process_task writes BLOCKED a second time, without a
    # message. The message of the first write stays.
    assert outcome.status == "BLOCKED"
    assert outcome.signal_message == (
        f"Cannot run {dependent} - 1 dependencies are failing: "
        f"{needs} ({S.ACTION_REQUIRED})."
    )


def test_a_failed_check_carries_the_fail_message(e2e_repo):
    workflow, session, registry = registered(e2e_repo, "my-actions")
    task = by_name(workflow)["NeedsApproval"]
    task.check = lambda: task.fail("the target is missing")

    ack = LocalSessionClient(session).check_tasks(keys=(task.identity_key,))
    workflow.runner.get_batch(ack.batch_uuid).wait()

    (row,) = LocalSessionClient(session).history()
    outcome = row.tasks[task.identity_key]
    assert outcome.status == "FAILED"
    assert outcome.signal_message == "the target is missing"


def test_a_block_from_a_gate_carries_the_block_message(e2e_repo):
    workflow, session, registry = registered(e2e_repo, "my-actions")
    client = LocalSessionClient(session)
    task = by_name(workflow)["NeedsApproval"]
    task.can_run = lambda: task.block("the maintenance window is closed")

    run_to_completion(workflow, client, task)

    (row,) = client.history()
    outcome = row.tasks[task.identity_key]
    assert outcome.status == "BLOCKED"
    assert outcome.signal_message == "the maintenance window is closed"


def test_a_gate_that_returns_false_carries_no_message(e2e_repo):
    workflow, session, registry = registered(e2e_repo, "my-actions")
    client = LocalSessionClient(session)
    task = by_name(workflow)["NeedsApproval"]
    task.can_run = lambda: False

    run_to_completion(workflow, client, task)

    (row,) = client.history()
    outcome = row.tasks[task.identity_key]
    assert outcome.status == "BLOCKED"
    assert outcome.signal_message == ""


def test_a_mirrored_status_does_not_inherit_a_message(e2e_repo):
    workflow, session, registry = registered(e2e_repo, "my-actions")
    task = by_name(workflow)["NeedsApproval"]
    store = ExecutionRecordStore(workflow.bus, "batch", [])
    store.register(task)
    workflow.runner._execution_record_store_map["batch"] = store
    key = task.identity_key

    store.settle(key, S.BLOCKED, "the gate is closed")
    workflow.runner._mirror_batch_status(task, S.COMPLETED, "batch")

    assert store.get_record(key).message_for(store[key]) == ""


def test_the_message_belongs_to_the_status_that_carried_it(e2e_repo):
    workflow, session, registry = registered(e2e_repo, "my-actions")
    task = by_name(workflow)["NeedsApproval"]
    store = ExecutionRecordStore(workflow.bus, "batch", [])
    store.register(task)
    key = task.identity_key
    record = store.get_record(key)

    store.settle(key, S.BLOCKED, "the gate is closed")
    assert store[key] is S.BLOCKED
    assert record.message_for(S.BLOCKED) == "the gate is closed"

    store.settle(key, S.BLOCKED)
    assert record.message_for(S.BLOCKED) == "the gate is closed"

    store.settle(key, S.BLOCKED, "another gate is closed")
    assert record.message_for(S.BLOCKED) == "another gate is closed"

    # A reader that holds an older status gets no message of another status.
    assert record.message_for(S.FAILED) == ""

    store.settle(key, S.CHECKING_COMPLETION)
    store.settle(key, S.BLOCKED)
    assert record.message_for(S.BLOCKED) == ""


def test_the_websocket_serves_the_prompt(e2e_repo):
    workflow, session, registry, task, _ = paused_session(e2e_repo)
    process = ServedProcess(registry).start()
    client = RemoteAppClient(process.url, token=TOKEN).connect()
    try:
        remote = client.session(session.session_id).history()
    finally:
        client.close()
        process.stop()

    assert remote == LocalSessionClient(session).history()
    (row,) = remote
    assert row.tasks[task.identity_key].signal_message == APPROVAL_PROMPT


def test_the_mcp_history_tool_serves_the_prompt(e2e_repo):
    workflow, session, registry, task, ack = paused_session(e2e_repo)

    (history,) = call_tools(
        mcp_app(registry), [("history", {"session_id": session.session_id})]
    )

    row = history[0] if isinstance(history, list) else history
    assert row["uuid"] == ack.batch_uuid
    outcome = row["tasks"][task.identity_key]
    assert outcome["status"] == "ACTION_REQUIRED"
    assert outcome["signal_message"] == APPROVAL_PROMPT
    (local,) = LocalSessionClient(session).history()
    assert outcome == asdict(local.tasks[task.identity_key])
