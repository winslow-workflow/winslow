"""The user on the records of a side effect: the ambient scope the edge
sets, the threads that inherit it, and the fields the batch, the manifest,
the cache record and the log record carry. An in-process client sets no
scope, so its records carry None."""

import json
import logging
import threading
import time
from argparse import Namespace
from contextlib import contextmanager

from starlette.testclient import TestClient

from winslow.cache import MISSING, JsonFileStorage, StorageRecord, WorkflowCache, entry
from winslow.client import LocalAppClient
from winslow.constants import Mode
from winslow.logger import ContextStampFilter
from winslow.model import BatchInfo, BatchOutcome
from winslow.orchestrator import Orchestrator, OrchestratorConfig
from winslow.protocol.frames import (
    AckFrame,
    ActionFrame,
    HelloFrame,
    HelloOkFrame,
    RequestFrame,
    ResultFrame,
)
from winslow.protocol.lanes import BatchCompletedLane
from winslow.serve import create_app
from winslow.session import Session, SessionRegistry
from winslow.access.permissions import CredentialKind, PermissionContext
from winslow.task.context import current_user, scoped_permissions
from winslow.ui.builtin_plugins.workflow.history import BatchView
from winslow.task.status import TaskStatus as S

from harness import TOKEN, serve_authenticator
from harness import (
    started,
    bare_orchestrator,
    build_workflow,
    by_name,
    run_batch,
    scratch_state_store,
    wait_for_status,
)
from test_serve_mcp import call_tools, mcp_app
from test_serve_mcp import registered as mcp_registered


def as_user(name, credential_id=None):
    """The scope of a request from `name`, with every permission."""
    return scoped_permissions(
        PermissionContext.full(name, CredentialKind.TICKET, credential_id=credential_id)
    )


def persisted(e2e_repo, state_store, name="my-workflow", **state_kwargs):
    workflow = build_workflow(e2e_repo, name, Mode.TUI)
    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    workflow.init_state(state_store, origin="test", **state_kwargs)
    return workflow, session


def manifest_payload(directory, session_id):
    return json.loads((directory / session_id / "manifest.json").read_text())


def history(workflow):
    runner = workflow.runner
    return [
        BatchOutcome.from_batch(batch, runner.record_store(batch.uuid))
        for batch in runner.batches
    ]


class StampedRecords(logging.Handler):
    """A handler that keeps the stamped records of the winslow loggers."""

    def __init__(self):
        super().__init__()
        self.addFilter(ContextStampFilter())
        self.records = []

    def emit(self, record):
        self.records.append(record)


def collecting_orchestrator(directory):
    config, unknown = Orchestrator.get_base_parser().parse_known_args(
        ["serve"], namespace=OrchestratorConfig()
    )
    orchestrator = Orchestrator(config, directory=directory, unknown_args=unknown)
    orchestrator.workflow_registry.collect_classes(directory)
    return orchestrator


# --- the scope ------------------------------------------------------------------


def test_the_scope_nests_and_resets():
    assert current_user() is None
    with as_user("can"):
        assert current_user() == "can"
        with as_user("lucas"):
            assert current_user() == "lucas"
        assert current_user() == "can"
    assert current_user() is None


def test_a_new_thread_starts_with_no_user():
    seen = []
    with as_user("can"):
        thread = threading.Thread(target=lambda: seen.append(current_user()))
        thread.start()
        thread.join()
    assert seen == [None]


def test_the_stamp_filter_carries_the_user_and_the_credential():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", (), None)
    with as_user("can", credential_id="4c1f"):
        ContextStampFilter().filter(record)
    assert (record.user, record.credential, record.credential_id) == (
        "can",
        "ticket",
        "4c1f",
    )
    ContextStampFilter().filter(record)
    assert (record.user, record.credential, record.credential_id) == (None, None, None)


# --- the batch ------------------------------------------------------------------


def test_a_local_run_leaves_no_user_on_the_batch(e2e_repo, state_store):
    workflow, session = persisted(e2e_repo, state_store)
    run_batch(workflow)
    workflow.persistence_listener.flush()
    (batch,) = workflow.runner.batches
    assert batch.user is None
    assert BatchInfo.from_batch(batch, workflow.tasks).user is None
    (row,) = history(workflow)
    assert row.user is None


def test_the_batch_worker_runs_as_the_submitter(e2e_repo, state_store):
    workflow, session = persisted(e2e_repo, state_store)
    alpha = by_name(workflow)["Alpha"]
    stamped = StampedRecords()
    logger = logging.getLogger("winslow")
    level = logger.level
    logger.addHandler(stamped)
    logger.setLevel(logging.INFO)
    try:
        with as_user("can", credential_id="4c1f"):
            batch = workflow.runner.submit_run([alpha])
        batch.wait()
    finally:
        logger.removeHandler(stamped)
        logger.setLevel(level)
    workflow.persistence_listener.flush()

    assert (batch.user, batch.credential, batch.credential_id) == (
        "can",
        "ticket",
        "4c1f",
    )
    info = BatchInfo.from_batch(batch, [alpha])
    assert (info.user, info.credential, info.credential_id) == ("can", "ticket", "4c1f")
    (row,) = history(workflow)
    assert (row.user, row.credential, row.credential_id) == ("can", "ticket", "4c1f")
    record_path = (
        state_store.open_directory
        / session.session_id
        / "batches"
        / batch.uuid
        / "record.json"
    )
    stored = json.loads(record_path.read_text())
    assert (stored["user"], stored["credential"], stored["credential_id"]) == (
        "can",
        "ticket",
        "4c1f",
    )
    # The run logged from the worker thread, under the copied scope.
    assert any(record.user == "can" and record.task_name for record in stamped.records)


def test_the_history_row_names_the_submitter_with_the_credential_id():
    def view(**fields):
        return BatchView(
            uuid="b",
            action="RUN",
            status="COMPLETED",
            task_count=1,
            created_at=0.0,
            options=None,
            entries=(),
            **fields,
        )

    assert view().submitter is None
    assert view(user="local", credential="local").submitter == "local"
    named = view(user="can", credential="token", credential_id="9f86d081")
    assert named.submitter == "can (token 9f86d081)"


# --- the session ----------------------------------------------------------------


def test_the_manifest_names_the_starter_and_the_ender(e2e_repo, state_store):
    workflow, session = persisted(e2e_repo, state_store, user="can")
    (manifest,) = state_store.list_open_manifests()
    assert manifest.user == "can"
    assert manifest.ended_by is None

    with as_user("lucas"):
        session.end()
    payload = manifest_payload(state_store.ended_directory, session.session_id)
    assert payload["user"] == "can"
    assert payload["ended_by"] == "lucas"


def test_a_session_that_ends_by_itself_names_no_ender(e2e_repo, state_store):
    workflow, session = persisted(e2e_repo, state_store, user="can")
    session.end()
    payload = manifest_payload(state_store.ended_directory, session.session_id)
    assert payload["ended_by"] is None


def test_create_session_stamps_the_ambient_user_and_a_restore_keeps_it(
    e2e_repo, state_store
):
    registry = SessionRegistry()
    port = LocalAppClient(
        registry,
        orchestrator=collecting_orchestrator(e2e_repo),
        state_store=state_store,
    )
    with as_user("can"):
        info = started(port.create_session("my-workflow"))
    assert info.user == "can"
    assert registry.resolve(info.session_id).user == "can"
    (manifest,) = state_store.list_open_manifests()
    assert manifest.user == "can"

    # The process dies: the registry forgets the session, the manifest stays open.
    registry.remove(info.session_id)
    port = LocalAppClient(
        SessionRegistry(),
        orchestrator=collecting_orchestrator(e2e_repo),
        state_store=state_store,
    )
    with as_user("lucas"):
        restored = started(port.restore_session(info.session_id))
    assert restored.user == "can"
    (manifest,) = state_store.list_open_manifests()
    assert manifest.user == "can"


# --- the cache ------------------------------------------------------------------


def test_a_cache_load_names_its_user():
    class Built(WorkflowCache):
        name = "built"

        @entry
        def values(self):
            return {"a": 1}

    cache = Built(Namespace(cache_namespace="wf-00000000"))
    with as_user("can"):
        assert cache.values == {"a": 1}
    assert cache.peek("values").written_by == "can"
    (info,) = cache.inspect()
    assert info.written_by == "can"


def test_the_json_file_round_trips_the_writer_and_reads_an_old_file_as_none(
    tmp_path,
):
    class TmpJsonStorage(JsonFileStorage):
        base_directory = tmp_path

    storage = TmpJsonStorage("built", "wf-00000000")
    stored = storage.write(
        "values", StorageRecord(value=[1], written_at=1.0, written_by="can")
    )
    assert stored.written_by == "can"
    assert storage.read("values").written_by == "can"

    (storage.directory / "old.json").write_text(
        json.dumps({"written_at": 1.0, "value": [1]})
    )
    old = storage.read("old")
    assert old is not MISSING
    assert old.written_by is None


# --- the two doors --------------------------------------------------------------


@contextmanager
def wire(registry):
    app = create_app(
        registry,
        serve_authenticator(),
        hello_timeout=1.0,
        orchestrator=bare_orchestrator(),
        state_store=scratch_state_store(),
    )
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_json({"type": HelloFrame.type, "token": TOKEN})
        assert ws.receive_json()["type"] == HelloOkFrame.type
        yield ws


def frames_until(ws, kind, limit=500):
    for _ in range(limit):
        frame = ws.receive_json()
        if frame["type"] == kind.type:
            return frame
    raise AssertionError(f"no {kind.type!r} frame within {limit} frames")


def test_the_websocket_stamps_the_hello_user(e2e_repo):
    workflow = build_workflow(e2e_repo, "my-workflow", Mode.TUI)
    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    registry = SessionRegistry()
    registry.register(session)
    alpha = by_name(workflow)["Alpha"]
    with wire(registry) as ws:
        ws.send_json({"type": "subscribe", "session_id": session.session_id})
        ws.send_json(
            {
                "type": ActionFrame.type,
                "request_id": "r-1",
                "session_id": session.session_id,
                "action": "run_tasks",
                "fields": {"keys": [alpha.identity_key]},
            }
        )
        ack = frames_until(ws, AckFrame)
        assert ack["ack"]["accepted"] is True
        completed = frames_until(ws, BatchCompletedLane)
        assert completed["info"]["user"] == "can"

        ws.send_json(
            {
                "type": RequestFrame.type,
                "request_id": "r-2",
                "kind": "history",
                "session_id": session.session_id,
            }
        )
        result = frames_until(ws, ResultFrame)
        assert [row["user"] for row in result["result"]] == ["can"]


def test_the_mcp_door_stamps_its_client_id(e2e_repo):
    workflow, session, registry = mcp_registered(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    (ack,) = call_tools(
        mcp_app(registry),
        [
            (
                "run_tasks",
                {"session_id": session.session_id, "keys": [alpha.identity_key]},
            )
        ],
    )
    assert ack["accepted"] is True
    wait_for_status(workflow, alpha, S.COMPLETED)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and session.active_batches:
        time.sleep(0.01)
    (row,) = history(workflow)
    assert row.user == "can"
