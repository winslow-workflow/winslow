"""Batch records: open on submit with the option snapshot and the roster,
closed with the final status, the per-task outcomes, and the log dump."""

import json
from dataclasses import asdict

from winslow.constants import Mode
from winslow.events import BatchCompletedEvent, BatchCreatedEvent
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
    # The close record rides the writer queue; land it before the read.
    workflow.persistence_listener.flush()

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
    # The close record rides the writer queue; land it before the read.
    workflow.persistence_listener.flush()

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
    # The close record rides the writer queue; land it before the read.
    workflow.persistence_listener.flush()

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


def test_batch_events_carry_values_only(e2e_repo, state_store):
    workflow, session = persisted_gates(e2e_repo, state_store)
    events = []
    workflow.subscribe(BatchCreatedEvent, events.append)
    workflow.subscribe(BatchCompletedEvent, events.append)

    run_batch(workflow)

    created, completed = events
    assert created.info.status == "RUNNING"
    assert completed.info.status == "FINISHED"
    for info in (created.info, completed.info):
        for name, value in asdict(info).items():
            assert isinstance(
                value, (str, int, float, dict, tuple, type(None))
            ), f"{name} is not a value: {value!r}"
    assert created.info.uuid == completed.info.uuid
    assert set(created.info.tasks) == {t.identity_key for t in workflow.tasks}
    assert created.info.options["dry_run"] is False


def test_the_close_record_lands_after_every_snapshot_of_the_batch(
    e2e_repo, state_store, monkeypatch
):
    # The invariant: a closed record implies durable outcomes. The writer
    # queue orders the close behind the snapshots, so the backend sees every
    # snapshot save first.
    writes = []
    save_snapshot = state_store.save_status_snapshot
    save_batch = state_store.save_batch

    def record_snapshot(session_id, entry):
        writes.append(("snapshot", entry.key))
        return save_snapshot(session_id, entry)

    def record_batch(record):
        writes.append(("batch", record.closed_status))
        return save_batch(record)

    monkeypatch.setattr(state_store, "save_status_snapshot", record_snapshot)
    monkeypatch.setattr(state_store, "save_batch", record_batch)

    workflow, session = persisted_gates(e2e_repo, state_store)
    run_batch(workflow)

    close_index = writes.index(("batch", "FINISHED"))
    snapshot_indices = [i for i, (kind, _) in enumerate(writes) if kind == "snapshot"]
    assert snapshot_indices and max(snapshot_indices) < close_index
