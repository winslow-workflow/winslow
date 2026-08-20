import json

import pytest

from winslow.exceptions import MisconfigurationError, SerializationError
from winslow.orchestrator import OrchestratorConfig
from winslow.state import (
    _BACKENDS,
    BatchRecord,
    FileStateStore,
    StatusSnapshot,
    SessionManifest,
    create_state_store,
    is_trusted,
    register_state_backend,
)

SID = "alpha-20260819T100000-aaaa0001"
OTHER_SID = "beta-20260819T110000-aaaa0002"


def build_manifest(session_id=SID, **overrides):
    fields = dict(
        session_id=session_id,
        workflow_class="AlphaWorkflow",
        workflow_namespace="alpha-00000000",
        orchestrator_overrides=None,
        workflow_values={"region": "emea"},
        origin="tui",
        started_at=1000.0,
    )
    fields.update(overrides)
    return SessionManifest(**fields)


def build_entry(key="task-0001", status="COMPLETED", checked_at=1000.0):
    return StatusSnapshot(key=key, status=status, checked_at=checked_at)


def build_batch(batch_uuid, session_id=SID, created_at=1000.0, **overrides):
    fields = dict(
        batch_uuid=batch_uuid,
        session_id=session_id,
        action="RUN",
        created_at=created_at,
        execution_options={"dry_run": False},
        tasks={"task-0001": "Alpha"},
    )
    fields.update(overrides)
    return BatchRecord(**fields)


def test_manifest_round_trip_and_layout(state_store):
    manifest = build_manifest()
    state_store.save_manifest(manifest)

    path = state_store.open_directory / SID / "manifest.json"
    assert json.loads(path.read_text())["workflow_values"] == {"region": "emea"}
    # The atomic publish leaves no temp file behind.
    assert list(path.parent.glob("*.tmp")) == []
    assert state_store.list_open_manifests() == [manifest]


def test_load_manifest_reads_one_session(state_store):
    manifest = build_manifest()
    state_store.save_manifest(manifest)

    assert state_store.load_manifest(SID) == manifest
    assert state_store.load_manifest(OTHER_SID) is None


def test_save_manifest_replaces_the_stored_manifest(state_store):
    state_store.save_manifest(build_manifest())
    updated = build_manifest(workflow_values={"region": "apac"})
    state_store.save_manifest(updated)
    assert state_store.list_open_manifests() == [updated]


def test_mark_ended_archives_the_whole_session(state_store):
    state_store.save_manifest(build_manifest())
    state_store.save_status_snapshot(SID, build_entry())
    state_store.save_batch(build_batch("b1"))

    state_store.mark_ended(SID)

    assert state_store.list_open_manifests() == []
    assert not (state_store.open_directory / SID).exists()
    archive = state_store.ended_directory / SID
    stamped = json.loads((archive / "manifest.json").read_text())
    assert stamped["ended_at"] > 0
    assert stamped["outcome"] == "ended"
    assert (archive / "tasks" / "task-0001.json").is_file()
    assert (archive / "batches" / "b1" / "record.json").is_file()


def test_mark_ended_without_a_session_is_a_no_op(state_store):
    state_store.mark_ended(SID)
    assert state_store.list_open_manifests() == []


def test_mark_errored_archives_the_session_as_failed(state_store):
    state_store.save_manifest(build_manifest())

    state_store.mark_errored(SID)

    assert state_store.list_open_manifests() == []
    assert not (state_store.open_directory / SID).exists()
    assert not (state_store.ended_directory / SID).exists()
    stamped = json.loads(
        (state_store.error_directory / SID / "manifest.json").read_text()
    )
    assert stamped["ended_at"] > 0
    assert stamped["outcome"] == "error"


def test_open_manifests_sort_by_session_id(state_store):
    state_store.save_manifest(build_manifest(OTHER_SID))
    state_store.save_manifest(build_manifest(SID))
    assert [m.session_id for m in state_store.list_open_manifests()] == [
        SID,
        OTHER_SID,
    ]


def test_a_corrupt_manifest_reads_as_missing(state_store):
    manifest = build_manifest()
    state_store.save_manifest(manifest)
    corrupt = state_store.open_directory / OTHER_SID / "manifest.json"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("{not json")

    assert state_store.list_open_manifests() == [manifest]


def test_a_torn_mark_ended_heals_on_the_next_listing(state_store):
    # A kill between the stamp and the archive leaves a stamped manifest in
    # open/. The listing hides it and finishes the relocation. No outcome
    # stamp: a manifest from before the error directory archives as ended.
    state_store.save_manifest(build_manifest(ended_at=2000.0))

    assert state_store.list_open_manifests() == []
    assert not (state_store.open_directory / SID).exists()
    assert (state_store.ended_directory / SID / "manifest.json").is_file()


def test_a_torn_mark_errored_heals_into_the_error_directory(state_store):
    # The outcome stamp names the target, so the relocation of a torn
    # mark_errored lands in error/, not in ended/.
    state_store.save_manifest(build_manifest(ended_at=2000.0, outcome="error"))

    assert state_store.list_open_manifests() == []
    assert (state_store.error_directory / SID / "manifest.json").is_file()
    assert not (state_store.ended_directory / SID).exists()


def test_a_manifest_with_an_unknown_field_reads_as_missing(state_store):
    # A record from a newer schema drops out of the restore offer instead of
    # crashing the listing.
    state_store.save_manifest(build_manifest())
    path = state_store.open_directory / SID / "manifest.json"
    payload = json.loads(path.read_text())
    payload["from_the_future"] = True
    path.write_text(json.dumps(payload))

    assert state_store.list_open_manifests() == []


def test_a_corrupt_snapshot_reads_as_missing(state_store):
    state_store.save_status_snapshot(SID, build_entry("task-a"))
    (state_store.open_directory / SID / "tasks" / "task-b.json").write_text("{not json")

    assert set(state_store.load_status_snapshots(SID)) == {"task-a"}


def test_a_corrupt_batch_record_reads_as_missing(state_store):
    state_store.save_batch(build_batch("b1"))
    corrupt = state_store.open_directory / SID / "batches" / "b2" / "record.json"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("{not json")

    assert [r.batch_uuid for r in state_store.load_open_batches(SID)] == ["b1"]


def test_a_failed_write_leaves_no_temp_file(state_store, monkeypatch):
    import winslow.state as state_module

    real = state_module.tempfile.NamedTemporaryFile

    class Exploding:
        def __init__(self, *args, **kwargs):
            self.inner = real(*args, **kwargs)
            self.name = self.inner.name

        def __enter__(self):
            self.inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self.inner.__exit__(*exc)

        def write(self, text):
            raise OSError("disk full")

    monkeypatch.setattr(state_module.tempfile, "NamedTemporaryFile", Exploding)
    with pytest.raises(OSError):
        state_store.save_manifest(build_manifest())

    assert list((state_store.open_directory / SID).glob("*.tmp")) == []


def test_manifest_rejects_a_non_serializable_value(state_store):
    manifest = build_manifest(workflow_values={"handle": object()})
    with pytest.raises(SerializationError, match="not JSON-serializable"):
        state_store.save_manifest(manifest)


def test_illegal_session_id_is_rejected(state_store):
    with pytest.raises(MisconfigurationError, match="illegal session id"):
        state_store.save_manifest(build_manifest(session_id="../evil"))


def test_illegal_task_key_is_rejected(state_store):
    with pytest.raises(MisconfigurationError, match="illegal task identity key"):
        state_store.save_status_snapshot(SID, build_entry(key="../evil"))


def test_a_snapshot_replaces_the_previous_one(state_store):
    state_store.save_status_snapshot(SID, build_entry("task-a", "FAILED", 1.0))
    state_store.save_status_snapshot(SID, build_entry("task-b", "COMPLETED", 2.0))
    state_store.save_status_snapshot(SID, build_entry("task-a", "COMPLETED", 3.0))

    snapshots = state_store.load_status_snapshots(SID)
    assert snapshots["task-a"] == build_entry("task-a", "COMPLETED", 3.0)
    assert snapshots["task-b"] == build_entry("task-b", "COMPLETED", 2.0)
    # One file per task: the latest snapshot is the whole content.
    assert len(list((state_store.open_directory / SID / "tasks").iterdir())) == 2


def test_snapshots_are_scoped_to_their_session(state_store):
    state_store.save_status_snapshot(SID, build_entry("task-a"))
    assert state_store.load_status_snapshots(OTHER_SID) == {}


def test_a_missing_session_loads_no_snapshots(state_store):
    assert state_store.load_status_snapshots(SID) == {}


def test_open_batches_filter_by_close_mark_and_sort(state_store):
    state_store.save_batch(build_batch("b2", created_at=2.0))
    state_store.save_batch(build_batch("b1", created_at=1.0))
    state_store.save_batch(build_batch("b3", session_id=OTHER_SID, created_at=3.0))
    closed = build_batch("b1", created_at=1.0, closed_status="FINISHED")
    state_store.save_batch(closed)

    open_batches = state_store.load_open_batches(SID)
    assert [record.batch_uuid for record in open_batches] == ["b2"]
    assert open_batches[0].closed_status is None
    assert [r.batch_uuid for r in state_store.load_open_batches(OTHER_SID)] == ["b3"]


def test_the_batch_record_carries_the_audit_fields(state_store):
    record = build_batch(
        "b1",
        execution_options={"dry_run": True},
        tasks={"task-a": "Alpha", "task-b": "Beta"},
        closed_status="FINISHED",
        completed_at=2000.0,
    )
    state_store.save_batch(record)

    path = state_store.open_directory / SID / "batches" / "b1" / "record.json"
    payload = json.loads(path.read_text())
    assert payload["execution_options"] == {"dry_run": True}
    assert payload["tasks"] == {"task-a": "Alpha", "task-b": "Beta"}
    assert payload["completed_at"] == 2000.0
    assert record.task_count == 2


def test_batch_logs_land_next_to_the_record(state_store):
    state_store.save_batch(build_batch("b1"))
    state_store.save_batch_logs(SID, "b1", {"task-0001": ["line one", "line two"]})

    path = (
        state_store.open_directory / SID / "batches" / "b1" / "logs" / "task-0001.log"
    )
    assert path.read_text() == "line one\nline two\n"


def test_a_missing_stamp_is_never_trusted():
    assert not is_trusted(None, 3600, 0.0, 100.0)
    assert not is_trusted(None, None, 0.0, 100.0)


def test_the_ttl_boundary_is_inclusive():
    assert is_trusted(1000.0, 60, 0.0, 1060.0)
    assert not is_trusted(1000.0, 60, 0.0, 1060.5)


def test_no_ttl_trusts_only_this_session():
    assert is_trusted(1000.0, None, 1000.0, 2000.0)
    assert not is_trusted(999.0, None, 1000.0, 2000.0)


def test_the_default_backend_is_the_file_store(monkeypatch):
    monkeypatch.delenv("WINSLOW_STATE_BACKEND", raising=False)
    assert isinstance(create_state_store(OrchestratorConfig()), FileStateStore)


def test_a_registered_backend_is_selectable(monkeypatch):
    class RecordingStore(FileStateStore):
        pass

    register_state_backend("recording", RecordingStore)
    try:
        monkeypatch.setenv("WINSLOW_STATE_BACKEND", "recording")
        assert isinstance(create_state_store(OrchestratorConfig()), RecordingStore)
    finally:
        _BACKENDS.pop("recording", None)


def test_an_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("WINSLOW_STATE_BACKEND", "nope")
    with pytest.raises(MisconfigurationError, match="'nope'"):
        create_state_store(OrchestratorConfig())
