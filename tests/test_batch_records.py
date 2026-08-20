"""Batch records: open on submit with the option snapshot and the roster,
closed with the final status, the per-task outcomes, and the log dump."""

import json

from winslow.constants import Mode
from winslow.session import Session
from winslow.task.status import TaskStatus as S

from harness import build_workflow, by_name, run_batch, start_gated_batch


def persisted_gates(e2e_repo, state_store):
    workflow = build_workflow(e2e_repo, "my-gates", Mode.TUI)
    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    workflow.init_state(state_store, origin="test")
    return workflow, session


def record_payload(state_store, session, batch):
    path = (
        state_store.open_directory
        / session.session_id
        / "batches"
        / batch.uuid
        / "record.json"
    )
    return json.loads(path.read_text())


def test_the_open_record_carries_options_and_roster(e2e_repo, state_store):
    workflow, session = persisted_gates(e2e_repo, state_store)
    gate, batch = start_gated_batch(workflow, by_name(workflow))

    (record,) = state_store.load_open_batches(session.session_id)
    assert record.batch_uuid == batch.uuid
    assert record.action == "RUN"
    assert record.execution_options["dry_run"] is False
    assert record.task_count == len(workflow.tasks)
    assert record.tasks == {task.identity_key: str(task) for task in workflow.tasks}

    gate.set()
    batch.wait()

    assert state_store.load_open_batches(session.session_id) == []


def test_the_close_stamps_outcomes_and_dumps_the_logs(
    e2e_repo, state_store, monkeypatch
):
    workflow, session = persisted_gates(e2e_repo, state_store)
    gated = by_name(workflow)["Gated"]
    original = type(gated).run

    def run(self):
        self.logger.warning("gated says hello")
        original(self)

    monkeypatch.setattr(type(gated), "run", run)
    gate, batch = start_gated_batch(workflow, by_name(workflow))
    gate.set()
    batch.wait()

    payload = record_payload(state_store, session, batch)
    assert payload["closed_status"] == "FINISHED"
    assert payload["completed_at"] >= payload["created_at"]

    logs_dir = (
        state_store.open_directory
        / session.session_id
        / "batches"
        / batch.uuid
        / "logs"
    )
    assert "gated says hello" in (logs_dir / f"{gated.identity_key}.log").read_text()


def test_a_stopped_batch_closes_stopped(e2e_repo, state_store):
    workflow, session = persisted_gates(e2e_repo, state_store)
    gate, batch = start_gated_batch(workflow, by_name(workflow))

    batch.request_stop()
    gate.set()
    batch.wait()

    payload = record_payload(state_store, session, batch)
    assert payload["closed_status"] == "STOPPED"
    assert state_store.load_open_batches(session.session_id) == []


def test_a_record_write_failure_does_not_refuse_the_batch(
    e2e_repo, state_store, mode, monkeypatch
):
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    workflow.init_state(state_store, origin="test")

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(state_store, "save_batch", explode)
    run_batch(workflow)

    # The batch ran to its outcomes although no record persisted.
    assert S.COMPLETED in set(workflow.store.values())
    assert state_store.load_open_batches(session.session_id) == []


def test_batch_records_close_in_both_modes(e2e_repo, state_store, mode):
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    workflow.init_state(state_store, origin="test")
    run_batch(workflow)

    assert state_store.load_open_batches(session.session_id) == []
    batches = state_store.open_directory / session.session_id / "batches"
    (record_dir,) = [d for d in batches.iterdir() if d.is_dir()]
    payload = json.loads((record_dir / "record.json").read_text())
    assert payload["closed_status"] == "FINISHED"
    assert set(payload["tasks"]) == {
        task.identity_key
        for task in workflow.tasks
        if workflow.store[task] is not S.SKIPPED
    }
