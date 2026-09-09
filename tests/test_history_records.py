"""Execution history holds values and identity keys, never a Task. These tests
cover the contract of history-spec.md: the release of every task at the
session end, the wire format of TaskInfo, the capture depths, and the
uuid-based execution events."""

import dataclasses
import gc
import json
import logging
import time
import weakref
from functools import cached_property

import pytest

from winslow.constants import Mode
from winslow.logger import RUNS_LOGGER_NAME
from winslow.events import ExecutionStatusEvent
from winslow.task.info import NOT_EVALUATED, TaskInfo, TaskRef
from winslow.task.status import TaskStatus as S
from winslow.task.task import Task

from harness import (
    build_workflow,
    by_name,
    gated_workflow,
    run_all,
    run_batch,
    start_gated_batch,
)


def _record_stores(workflow):
    return workflow.runner.record_stores()


def _wait_for_store(workflow, batch, timeout=5.0):
    """Record stores are born on the worker thread, so poll for arrival."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        store = workflow.runner.record_store(batch.uuid)
        if store is not None:
            return store
        time.sleep(0.005)
    raise AssertionError(f"no record store for batch {batch.uuid[:8]}")


@pytest.fixture
def isolated_winslow_logs():
    """Restore the prod propagate=False boundary (setup_run_logging): without
    it the pytest capture keeps records whose exc_info retains the tasks."""
    loggers = [logging.getLogger(RUNS_LOGGER_NAME), logging.getLogger("winslow")]
    before = [(logger, logger.propagate) for logger in loggers]
    for logger in loggers:
        logger.propagate = False
    yield
    for logger, propagate in before:
        logger.propagate = propagate


def test_session_end_frees_every_task_while_history_stays(
    e2e_repo, isolated_winslow_logs
):
    """The test of the feature: after the session ends, no history structure
    keeps a task alive, and the batch records stay browsable as values."""
    workflow = build_workflow(e2e_repo, "my-workflow", Mode.TUI)
    run_all(workflow)

    refs = [weakref.ref(task) for task in workflow.tasks]
    stores = _record_stores(workflow)
    assert stores, "the run must leave a batch in history"

    workflow.session.end()
    assert workflow.session.has_ended
    gc.collect()

    assert all(ref() is None for ref in refs), "a task survived the session end"
    # History still renders: value records with their final statuses.
    for store in stores:
        assert store.records
        assert all(isinstance(record.info, TaskInfo) for record in store.records)
        assert all(isinstance(status, S) for status in store.values())


def test_session_end_frees_tasks_after_a_reraise(e2e_repo, isolated_winslow_logs):
    """A batch stores the reraise error for wait(). The session end must
    release it and keep its type and message (see ExecutionBatch.release_error)."""
    workflow = build_workflow(
        e2e_repo, "my-errors", Mode.TUI, "--reraise-errors", "--disable-concurrency"
    )
    with pytest.raises(Exception):
        run_all(workflow)

    refs = [weakref.ref(task) for task in workflow.tasks]
    batches = workflow.runner.batches
    assert any(batch._error is not None for batch in batches)

    workflow.session.end()
    gc.collect()

    assert all(ref() is None for ref in refs), "a task survived the session end"
    assert any(batch._error is not None for batch in batches)


def test_session_end_frees_tasks_after_an_attribute_error(
    e2e_repo, isolated_winslow_logs
):
    """AttributeError.obj holds the object of the failed lookup, which is the
    task. The release must detach it too (see ExecutionBatch.release_error)."""
    workflow = build_workflow(
        e2e_repo,
        "my-attr-errors",
        Mode.TUI,
        "--reraise-errors",
        "--disable-concurrency",
    )
    with pytest.raises(AttributeError):
        run_all(workflow)

    refs = [weakref.ref(task) for task in workflow.tasks]
    batches = workflow.runner.batches
    assert any(isinstance(batch._error, AttributeError) for batch in batches)

    workflow.session.end()
    gc.collect()

    assert all(ref() is None for ref in refs), "a task survived the session end"
    assert any(isinstance(batch._error, AttributeError) for batch in batches)


def test_task_info_asdict_json_round_trips(e2e_repo):
    """The wire-format contract: every field of a captured TaskInfo is a plain
    value, at both capture depths."""
    workflow = build_workflow(e2e_repo, "my-params", Mode.TUI)
    task = workflow.tasks[0]

    for info in (TaskInfo.from_task(task), TaskInfo.from_task(task, full=True)):
        payload = json.loads(json.dumps(dataclasses.asdict(info)))
        assert payload["key"] == task.identity_key
        assert payload["label"] == str(task)


def test_dependency_refs_carry_key_and_label(e2e_repo):
    workflow = build_workflow(e2e_repo, "my-depends", Mode.TUI)
    with_deps = next(t for t in workflow.tasks if t.dependent_tasks)

    info = TaskInfo.from_task(with_deps)
    refs = info.dependencies + info.premier_dependencies + info.terminal_dependencies
    assert refs
    by_key = {dep.identity_key: dep for dep in with_deps.dependent_tasks}
    for ref in refs:
        assert isinstance(ref, TaskRef)
        assert ref.label == str(by_key[ref.key])


def test_record_store_holds_no_task(e2e_repo):
    workflow = build_workflow(e2e_repo, "my-workflow", Mode.TUI)
    run_all(workflow)

    for store in _record_stores(workflow):
        assert all(isinstance(key, str) for key in store)
        for record in store.records:
            assert isinstance(record.info, TaskInfo)
            assert not hasattr(record, "task")


def test_errored_holds_identity_keys(e2e_repo):
    workflow = build_workflow(e2e_repo, "my-errors", Mode.TUI)
    tasks = by_name(workflow)

    run_all(workflow)

    batch = workflow.runner.batches[0]
    assert tasks["Boom"].identity_key in batch.errored
    assert all(isinstance(item, str) for item in batch.errored)


def test_completion_sweep_replaces_stub_with_full_capture(e2e_repo):
    """A record starts as a stub and the batch-completion sweep replaces it
    with a full capture, whose values are safe strings."""
    workflow, tasks = gated_workflow(e2e_repo)
    gate, batch = start_gated_batch(workflow, tasks)
    store = _wait_for_store(workflow, batch)

    record = store.get_record(tasks["Gated"].identity_key)
    assert record.info.attributes is None, "a stub before the sweep"

    gate.set()
    batch.wait()

    assert record.info.attributes is not None, "a full capture after the sweep"
    for title, columns, rows in record.info.attributes:
        for row in rows:
            assert all(isinstance(cell, str) for cell in row)


class _Probed(Task):
    """A sentinel task: each getter records its call. An automatic capture
    that runs one of them is the regression this class exists to catch."""

    calls = []

    @property
    def probe(self):
        self.calls.append("probe")
        return 1

    @cached_property
    def cached_probe(self):
        self.calls.append("cached_probe")
        return 2

    def run(self):
        pass


def _section(info, title):
    return dict(
        (name, value)
        for section_title, columns, rows in info.attributes
        if section_title == title
        for *_, name, value in [row[-2:] for row in rows]
    )


def test_automatic_capture_never_evaluates_a_getter():
    task = _Probed(workflow_config=None)
    _Probed.calls.clear()

    TaskInfo.from_task(task)
    full = TaskInfo.from_task(task, full=True)
    assert _Probed.calls == []
    assert _section(full, "Property Methods")["probe"] == NOT_EVALUATED
    assert _section(full, "Cached Property Methods")["cached_probe"] == NOT_EVALUATED

    # A value that the task itself materialized is captured, still with no
    # getter call from the capture.
    assert task.cached_probe == 2
    materialized = TaskInfo.from_task(task, full=True)
    assert _Probed.calls == ["cached_probe"]
    assert _section(materialized, "Cached Property Methods")["cached_probe"] == "2"

    # The on-demand capture is the single point that evaluates.
    evaluated = TaskInfo.from_task(task, evaluate=True)
    assert "probe" in _Probed.calls
    assert _section(evaluated, "Property Methods")["probe"] == "1"


def test_sanitize_passes_a_plain_record_through():
    """A plain record is returned as it is; a record with an exception becomes
    a text-only copy, and the original keeps its exc_info for the sinks."""
    from winslow.logger import TaskLogDispatcher

    plain = logging.LogRecord("n", logging.INFO, "p", 1, "plain", (), None)
    assert TaskLogDispatcher._sanitize(plain) is plain

    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        exc_info = sys.exc_info()
    rich = logging.LogRecord("n", logging.ERROR, "p", 1, "failed", (), exc_info)
    sanitized = TaskLogDispatcher._sanitize(rich)
    assert sanitized is not rich
    assert sanitized.exc_info is None
    assert "ValueError: boom" in sanitized.exc_text
    assert rich.exc_info is exc_info


def test_buffered_record_with_object_extra_cannot_retain_it():
    """A record logged with an object in `extra` is coerced to text in the
    buffer, so a task in a custom attribute cannot outlive its release."""
    import collections

    from winslow.logger import TaskLogDispatcher

    class _Payload:
        pass

    payload = _Payload()
    ref = weakref.ref(payload)

    dispatcher = TaskLogDispatcher()
    buffer = collections.deque()
    dispatcher.register_buffer("t-1", buffer)
    record = logging.LogRecord("n", logging.INFO, "p", 1, "plain", (), None)
    record.task_id = "t-1"
    record.payload = payload
    dispatcher.emit(record)

    stored = buffer[0]
    assert stored is not record
    assert isinstance(stored.payload, str)

    del payload, record
    gc.collect()
    assert ref() is None, "the buffered record retained the extra object"


def test_history_search_refuses_a_builtin_filter_subclass():
    """The search gate is by exact type: a project subclass of a builtin
    filter can touch live-task API, so history must refuse it."""
    from winslow.filter.builtin import GroupFilter, NameFilter
    from winslow.ui.builtin_plugins.workflow.history import _foreign_filter_names

    class ProjectFilter(NameFilter):
        long_command = "project"

    filters = [NameFilter("a"), GroupFilter("b"), ProjectFilter("c")]
    assert _foreign_filter_names(filters) == ["project"]


class _EventRecorder:
    def __init__(self):
        self.statuses = []

    def on_execution_status(self, event):
        self.statuses.append((event.batch_uuid, event.task_key, event.status))


def test_execution_events_route_by_batch_and_task_key(e2e_repo):
    workflow = build_workflow(e2e_repo, "my-workflow", Mode.TUI)
    recorder = _EventRecorder()
    workflow.bus.subscribe(ExecutionStatusEvent, recorder.on_execution_status)

    run_all(workflow)
    run_batch(workflow, workflow.tasks)

    assert recorder.statuses
    batch_uuids = {batch.uuid for batch in workflow.runner.batches}
    task_keys = {task.identity_key for task in workflow.tasks}
    for batch_uuid, task_key, _ in recorder.statuses:
        assert batch_uuid in batch_uuids
        assert task_key in task_keys
    # One subscription covers every batch: the events of both batches arrive.
    assert len({batch_uuid for batch_uuid, _, _ in recorder.statuses}) == 2
