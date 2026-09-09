"""Durable session state: one directory per session, plus its task snapshots.

A StateStore backend owns every durable write. FileStateStore is the default
backend. A package registers another backend with register_state_backend and
a deployment selects one with WINSLOW_STATE_BACKEND.
"""

import collections
import json
import os
import queue
import re
import tempfile
import threading
import time

from dataclasses import asdict, dataclass, replace
from functools import cached_property
from pathlib import Path

from winslow.events import (
    BatchCompletedEvent,
    BatchCreatedEvent,
    LogLineEvent,
    Origin,
    TaskStatusEvent,
)
from winslow.exceptions import (
    MisconfigurationError,
    SerializationError,
)
from winslow.logger import LOGGER
from winslow.settings import EXECUTION_RECORD_LOG_BUFFER_SIZE, config
from winslow.task.status import PASSING_STATUSES, SNAPSHOT_STATUSES, TaskStatus


# One path component under the state root. No leading dot and no separator,
# so a "..", a hidden name and an absolute path all fail.
_PATH_COMPONENT_PATTERN = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*\Z")


def _validate_path_component(kind, component):
    """Reject an identifier that could escape the state root. The framework
    stamps are safe by construction; this guards a hand-built one."""
    if not _PATH_COMPONENT_PATTERN.match(str(component)):
        raise MisconfigurationError(
            f"State store: illegal {kind} {component!r} - it must match "
            f"[A-Za-z0-9_-][A-Za-z0-9_.-]*."
        )


@dataclass(frozen=True)
class SessionManifest:
    """The inputs that rebuild a session (see Orchestrator.initialize_workflow).
    ended_at and outcome are None while the session is open; the mark of the
    end stamps them (see StateStore.mark_ended and StateStore.mark_errored)."""

    session_id: str
    workflow_class: str
    workflow_namespace: str
    orchestrator_overrides: dict | None
    workflow_values: dict | None
    origin: str
    started_at: float
    ended_at: float | None = None
    outcome: str | None = None


@dataclass(frozen=True)
class StatusSnapshot:
    """The latest snapshot of one task in one session. The key is the task
    identity key; the status is a TaskStatus name; checked_at is a wall-clock
    epoch."""

    key: str
    status: str
    checked_at: float


@dataclass(frozen=True)
class BatchRecord:
    """One batch of one session, with what an audit needs: the action, the
    option snapshot, and the task roster. closed_status is None while the
    batch runs."""

    batch_uuid: str
    session_id: str
    action: str
    created_at: float
    # The batch options that the batch snapshots at its start (see BatchOptions).
    execution_options: dict | None = None
    # The roster, {identity key: label}. Restore reads it to mark the tasks
    # of a dead batch; the snapshots hold the task statuses.
    tasks: dict | None = None
    closed_status: str | None = None
    completed_at: float | None = None

    @property
    def task_count(self):
        return len(self.tasks or {})


class StateStore:
    """The persistence contract of winslow. A backend only stores records:
    the callers own every policy decision (see BaseStorage). Each backend
    receives the orchestrator config at creation (see
    TelemetryConfiguration.get_handler for the same pattern)."""

    def __init__(self, orchestrator_config):
        self.orchestrator_config = orchestrator_config

    def save_manifest(self, manifest):
        """Store the SessionManifest, keyed by its session id. A second save
        with the same session id replaces the stored manifest."""
        raise NotImplementedError

    def load_manifest(self, session_id):
        """Return the stored SessionManifest of the session, or None."""
        raise NotImplementedError

    def mark_ended(self, session_id):
        """Stamp ended_at on the manifest and archive the session. A session
        with no stored state is not an error."""
        raise NotImplementedError

    def mark_errored(self, session_id):
        """Stamp ended_at on the manifest and archive the session as failed,
        apart from the sessions that ended. A session with no stored state is
        not an error."""
        raise NotImplementedError

    def list_open_manifests(self):
        """Return the manifests with no ended_at stamp, sorted by session id.
        These are the restore candidates."""
        raise NotImplementedError

    def save_status_snapshot(self, session_id, entry):
        """Store the StatusSnapshot of one task, replacing its previous snapshot.
        The write must stay cheap: it runs once per terminal transition."""
        raise NotImplementedError

    def load_status_snapshots(self, session_id):
        """Return {identity_key: StatusSnapshot} of the session."""
        raise NotImplementedError

    def save_batch(self, record):
        """Store the BatchRecord, keyed by its session id and batch uuid. A
        second save replaces the stored record: the close stamps the record
        by saving it again."""
        raise NotImplementedError

    def load_open_batches(self, session_id):
        """Return the records of the session with no closed_status, sorted by
        created_at. Restore marks these INTERRUPTED."""
        raise NotImplementedError

    def save_batch_logs(self, session_id, batch_uuid, logs_by_key):
        """Store the captured log lines of one batch, a list of lines per
        task identity key."""
        raise NotImplementedError


class FileStateStore(StateStore):
    """The default backend: one directory per session under WINSLOW_STATE_DIR.
    open/ holds the live sessions, ended/ the archive, and error/ the failed
    sessions. Writes are strict and atomic; a corrupt or unreadable file reads
    as missing."""

    # None resolves to the WINSLOW_STATE_DIR setting. A test or a project can
    # override it on a subclass. The default is relative to the CWD.
    base_directory = None

    def __init__(self, orchestrator_config):
        super().__init__(orchestrator_config)
        base = Path(
            self.base_directory or config("WINSLOW_STATE_DIR", default=".winslow/state")
        )
        self.open_directory = base / "open"
        self.ended_directory = base / "ended"
        self.error_directory = base / "error"

    @classmethod
    def _encode(cls, record):
        """Strict on purpose: a default=str fallback would coerce silently,
        and a lossy state file is not debuggable."""
        try:
            return json.dumps(asdict(record))
        except TypeError as exc:
            raise SerializationError(
                f"State record {record!r} is not JSON-serializable ({exc})."
            ) from exc

    @classmethod
    def _write_text(cls, path, text):
        """Publish atomically. A private temp name per writer: two processes
        that write the same path cannot publish each other's bytes."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = tempfile.NamedTemporaryFile(
            "w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8"
        )
        try:
            with temp:
                temp.write(text)
            os.replace(temp.name, path)
        except BaseException:
            # A failed write must not leave its temp file behind.
            Path(temp.name).unlink(missing_ok=True)
            raise

    @classmethod
    def _read_record(cls, path, record_class):
        """Return the decoded record, or None. A corrupt or unreadable file
        reads as missing, like a cache file (see JsonFileStorage)."""
        try:
            return record_class(**json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError):
            LOGGER.error(
                f"State file {path} is unreadable - treating the record as missing.",
                exc_info=True,
            )
            return None

    def _session_dir(self, session_id):
        _validate_path_component("session id", session_id)
        return self.open_directory / session_id

    def _manifest_path(self, session_id):
        return self._session_dir(session_id) / "manifest.json"

    def _snapshot_path(self, session_id, key):
        _validate_path_component("task identity key", key)
        return self._session_dir(session_id) / "tasks" / f"{key}.json"

    def _batch_dir(self, session_id, batch_uuid):
        _validate_path_component("batch uuid", batch_uuid)
        return self._session_dir(session_id) / "batches" / batch_uuid

    def _archive(self, session_dir, target_directory):
        target_directory.mkdir(parents=True, exist_ok=True)
        os.rename(session_dir, target_directory / session_dir.name)

    def save_manifest(self, manifest):
        self._write_text(
            self._manifest_path(manifest.session_id), self._encode(manifest)
        )

    def load_manifest(self, session_id):
        return self._read_record(self._manifest_path(session_id), SessionManifest)

    def mark_ended(self, session_id):
        self._mark(session_id, "ended", self.ended_directory)

    def mark_errored(self, session_id):
        self._mark(session_id, "error", self.error_directory)

    def _mark(self, session_id, outcome, target_directory):
        """Stamp first, archive second. A kill between the two steps leaves a
        stamped manifest in open/, which list_open_manifests relocates. The
        outcome stamp names the directory that the relocation targets."""
        session_dir = self._session_dir(session_id)
        path = self._manifest_path(session_id)
        manifest = self.load_manifest(session_id)
        if manifest is None:
            return
        self._write_text(
            path,
            self._encode(
                replace(manifest, ended_at=time.time(), outcome=outcome)
            ),
        )
        self._archive(session_dir, target_directory)

    def list_open_manifests(self):
        if not self.open_directory.is_dir():
            return []
        manifests = []
        for session_dir in sorted(self.open_directory.iterdir()):
            if not session_dir.is_dir():
                continue
            manifest = self._read_record(session_dir / "manifest.json", SessionManifest)
            if manifest is None:
                continue
            if manifest.ended_at is not None:
                self._archive_stray(session_dir, manifest)
                continue
            manifests.append(manifest)
        return manifests

    def _archive_stray(self, session_dir, manifest):
        """Finish a torn mark: the manifest is stamped, so only the relocation
        is missing. A failure is logged: the stamp already hides the session
        from every listing."""
        target = (
            self.error_directory
            if manifest.outcome == "error"
            else self.ended_directory
        )
        try:
            self._archive(session_dir, target)
        except OSError:
            LOGGER.error(
                f"Could not archive the ended session {session_dir.name}",
                exc_info=True,
            )

    def save_status_snapshot(self, session_id, entry):
        self._write_text(self._snapshot_path(session_id, entry.key), self._encode(entry))

    def load_status_snapshots(self, session_id):
        directory = self._session_dir(session_id) / "tasks"
        if not directory.is_dir():
            return {}
        entries = (
            self._read_record(path, StatusSnapshot)
            for path in sorted(directory.glob("*.json"))
        )
        return {entry.key: entry for entry in entries if entry is not None}

    def save_batch(self, record):
        path = self._batch_dir(record.session_id, record.batch_uuid) / "record.json"
        self._write_text(path, self._encode(record))

    def load_open_batches(self, session_id):
        directory = self._session_dir(session_id) / "batches"
        if not directory.is_dir():
            return []
        records = (
            self._read_record(batch_dir / "record.json", BatchRecord)
            for batch_dir in sorted(directory.iterdir())
            if batch_dir.is_dir()
        )
        return sorted(
            (r for r in records if r is not None and r.closed_status is None),
            key=lambda record: record.created_at,
        )

    def save_batch_logs(self, session_id, batch_uuid, logs_by_key):
        directory = self._batch_dir(session_id, batch_uuid) / "logs"
        for key, lines in logs_by_key.items():
            _validate_path_component("task identity key", key)
            self._write_text(directory / f"{key}.log", "\n".join(lines) + "\n")


def is_trusted(checked_at, ttl, session_start, now):
    """The one trust rule: a verification younger than its TTL counts; with
    no TTL only a verification of this session counts. The check gate, the
    restore seeding and the sweeper all apply it."""
    if checked_at is None:
        return False
    if ttl is not None:
        return now - checked_at <= ttl
    return checked_at >= session_start


# The shutdown marker of the writer thread (see SessionPersistenceAdapter).
_STOP = object()


@dataclass(frozen=True)
class _BatchClose:
    """A close record on the writer queue. Queue order puts it behind every
    snapshot of the batch, so a closed record implies durable outcomes."""

    record: "BatchRecord"
    logs: dict | None


class SessionPersistenceAdapter:
    """The single path of a task status onto and off persistence. The
    callback only queues, so it stays cheap on the writing thread, and the
    writer thread lands the write. A read overlays the writes of this session
    on the initial state. The subscriber acts only on a live transition: a
    SEED write re-applies a stored value (see Origin)."""

    def __init__(self, state_store, session_id):
        self._state_store = state_store
        self.session_id = session_id
        # The snapshots this session wrote, by key. get() reads them over the
        # initial state, so a fresh stamp is visible before its write lands.
        self._written = {}
        # {batch uuid: {task key: lines}}, gathered from the log events and
        # written with the close record of the batch.
        self._batch_logs = {}
        self._queue = queue.Queue()
        self._closed = False
        self._writer = threading.Thread(
            target=self._drain_queue, name=f"state-{session_id}", daemon=True
        )
        self._writer.start()

    @cached_property
    def initial_state(self):
        """{key: StatusSnapshot} as persisted before this session wrote: the
        one backend read. Only this session writes its directory, so one read
        covers the whole session."""
        return self._state_store.load_status_snapshots(self.session_id)

    def get(self, key):
        """The latest snapshot of the key, or None: a write of this session
        wins over the initial state."""
        entry = self._written.get(key)
        return entry if entry is not None else self.initial_state.get(key)

    # The adapter is the only face of persistence: it owns the backend and
    # the session id, so a caller passes neither.

    def save_batch(self, record):
        self._state_store.save_batch(record)

    def save_batch_logs(self, batch_uuid, logs):
        self._state_store.save_batch_logs(self.session_id, batch_uuid, logs)

    def load_open_batches(self):
        return self._state_store.load_open_batches(self.session_id)

    def load_manifest(self):
        return self._state_store.load_manifest(self.session_id)

    def save_manifest(self, manifest):
        self._state_store.save_manifest(manifest)

    def mark_errored(self):
        self._state_store.mark_errored(self.session_id)

    def mark_ended(self):
        self._state_store.mark_ended(self.session_id)

    def _subscriptions(self):
        return (
            (TaskStatusEvent, self.on_task_status),
            (BatchCreatedEvent, self.on_batch_created),
            (BatchCompletedEvent, self.on_batch_completed),
            (LogLineEvent, self.on_log_line),
        )

    def attach(self, workflow):
        """Wire each handler onto its session event (see TuiStoreAdapter for
        the same pattern on the UI side)."""
        for event, handler in self._subscriptions():
            workflow.subscribe(event, handler)

    def detach(self, workflow):
        """Disconnect every handler. The rollback of a failed registration
        calls this (see Workflow.init_state)."""
        for event, handler in self._subscriptions():
            workflow.unsubscribe(event, handler)

    def on_task_status(self, event):
        if event.origin is not Origin.RUN:
            return
        if event.status not in SNAPSHOT_STATUSES or self._closed:
            return
        # The callback stamps checked_at now, so queue latency cannot move it.
        entry = StatusSnapshot(
            key=event.key, status=event.status.name, checked_at=time.time()
        )
        self._written[event.key] = entry
        self._queue.put(entry)

    def _batch_record(self, info):
        """The audit record of one batch, from the event value: the action,
        the option snapshot, and the roster (see BatchInfo)."""
        return BatchRecord(
            batch_uuid=info.uuid,
            session_id=self.session_id,
            action=info.action,
            created_at=info.created_at,
            execution_options=info.options,
            tasks=info.tasks,
        )

    def on_batch_created(self, event):
        """A record with no close mark seeds as INTERRUPTED on restore. The
        write runs on the dispatch of the event: on the submitter thread,
        before any task work, so a crash leaves the open record behind."""
        if self._closed:
            return
        self._state_store.save_batch(self._batch_record(event.info))

    def on_log_line(self, event):
        if self._closed:
            return
        logs = self._batch_logs.setdefault(event.batch_uuid, {})
        lines = logs.setdefault(
            event.task_key,
            collections.deque(maxlen=EXECUTION_RECORD_LOG_BUFFER_SIZE),
        )
        lines.append(event.line)

    def on_batch_completed(self, event):
        """Queue the close record behind the snapshots of the batch. Queue
        order guarantees that a closed record implies durable outcomes."""
        if self._closed:
            return
        info = event.info
        closed = replace(
            self._batch_record(info),
            closed_status=info.status,
            completed_at=info.completed_at,
        )
        self._queue.put(_BatchClose(closed, self._batch_logs.pop(info.uuid, None)))

    def _drain_queue(self):
        while True:
            entry = self._queue.get()
            try:
                if entry is _STOP:
                    return
                if isinstance(entry, _BatchClose):
                    self._save_batch_close(entry)
                else:
                    self._state_store.save_status_snapshot(self.session_id, entry)
            except Exception:
                LOGGER.error(f"Could not store {entry}", exc_info=True)
            finally:
                self._queue.task_done()

    def _save_batch_close(self, entry):
        self._state_store.save_batch(entry.record)
        if entry.logs:
            self._state_store.save_batch_logs(
                self.session_id,
                entry.record.batch_uuid,
                {key: list(lines) for key, lines in entry.logs.items()},
            )

    def flush(self):
        """Block until every queued write has landed on the store. The close
        of a batch flushes, so a closed record implies durable snapshots."""
        if not self._closed:
            self._queue.join()

    def close(self):
        """Flush and stop the writer thread. The session end calls this
        before the archive move, so no write lands after the move."""
        if self._closed:
            return
        self._queue.join()
        self._closed = True
        self._queue.put(_STOP)


class StaleSweeper:
    """Flips a passing status to STALE when its check TTL lapses. The thread
    sleeps until the next expiry; a status write wakes it, because the write
    can move that deadline. The flip is an ordinary store write, and STALE
    never persists (see SNAPSHOT_STATUSES)."""

    def __init__(self, workflow):
        self.workflow = workflow
        self._wake = threading.Event()
        self._closed = False
        self._thread = threading.Thread(
            target=self._loop, name=f"stale-{workflow.session_id}", daemon=True
        )
        self._thread.start()

    def on_task_status(self, event):
        self._wake.set()

    def _loop(self):
        while not self._closed:
            try:
                deadline = self._sweep()
            except Exception:
                # The sweep races the session teardown; a failed pass skips.
                LOGGER.debug("The stale sweep failed and skips.", exc_info=True)
                deadline = None
            timeout = None if deadline is None else max(deadline - time.time(), 0.0)
            self._wake.wait(timeout)
            self._wake.clear()

    def _sweep(self):
        """Flip each expired passing status. Return the next expiry, or None
        when no passing task has a TTL."""
        workflow = self.workflow
        deadline = None
        now = time.time()
        for task in workflow.tasks:
            if workflow.store[task] not in PASSING_STATUSES:
                continue
            ttl = workflow.effective_check_ttl(task)
            if ttl is None:
                continue
            entry = workflow.load_snapshot(task.identity_key)
            if entry is None:
                continue
            expiry = entry.checked_at + ttl
            if expiry <= now:
                workflow.runner.set_status(task, TaskStatus.STALE, None)
            elif deadline is None or expiry < deadline:
                deadline = expiry
        return deadline

    def close(self):
        """Stop the thread. The session end calls this with the rest of the
        persistence teardown (see Workflow.archive_state)."""
        self._closed = True
        self._wake.set()


_BACKENDS = {"file": FileStateStore}


def register_state_backend(name, store_class):
    """Register a StateStore backend under a WINSLOW_STATE_BACKEND name.
    Register at import time, before create_state_store runs."""
    _BACKENDS[name] = store_class


def create_state_store(orchestrator_config):
    """Build the backend that WINSLOW_STATE_BACKEND selects (default file).
    The backend receives the orchestrator config of the run."""
    name = config("WINSLOW_STATE_BACKEND", default="file")
    if name not in _BACKENDS:
        raise MisconfigurationError(
            f"WINSLOW_STATE_BACKEND={name!r} names no registered state "
            f"backend. The registered backends are {sorted(_BACKENDS)}. "
            f"A package adds one with register_state_backend()."
        )
    return _BACKENDS[name](orchestrator_config)
