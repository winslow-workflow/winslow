import collections
import threading
import uuid as _uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Optional, TYPE_CHECKING

from winslow.settings import EXECUTION_RECORD_LOG_BUFFER_SIZE

if TYPE_CHECKING:
    from winslow.task.context import TaskExecutionContext
    from winslow.model import TaskInfo
    from winslow.runner.store import ExecutionRecordStore


class ExecutionAction(Enum):
    RUN = "run"
    CHECK = "check"


class ExecutionStatus(Enum):
    QUEUED = auto()
    RUNNING = auto()
    FINISHED = auto()
    STOPPED = auto()
    # A framework error aborted the batch before it could finish its tasks
    # (see ExecutionBatch.complete). The error message rides BatchInfo.
    ERRORED = auto()
    # The process died while the batch ran: a restore found the record open
    # in the state store. Only a restore writes it, into history.
    INTERRUPTED = auto()

    def __str__(self):
        return self.name.replace("_", " ")


class ExecutionPhase(Enum):
    """The steps of a task in one batch.

    This enum controls the cache lifecycle of the transient properties (see
    TransientProperty and BaseRunner.task_scope). Each checkability gate opens a
    new logical pass and resets the cache. The phases after the gate inherit the
    pass. A standalone check, such as a check action or a dependency probe, is
    one pass. The run flow has two passes: the pre-run gate opens the pass for
    the runnability and the run, and the post-run gate opens the separate
    verification pass.

    The declaration order is the run order. Consumers iterate the enum to sort by
    execution order, for example the transients table of the task detail."""

    # check only flow
    CHECKABILITY = "checkability"
    CHECK = "check"

    # run flow
    PRE_RUN_CHECKABILITY = "pre_run_checkability"
    PRE_RUN_CHECK = "pre_run_check"
    RUNNABILITY = "runnability"
    RUN = "run"
    POST_RUN_CHECKABILITY = "post_run_checkability"
    POST_RUN_CHECK = "post_run_check"

    @property
    def resets_cache(self):
        return self in (
            ExecutionPhase.CHECKABILITY,
            ExecutionPhase.PRE_RUN_CHECKABILITY,
            ExecutionPhase.POST_RUN_CHECKABILITY,
        )

    @property
    def checkability(self):
        """The gate phase that opens the pass of this check phase."""
        return {
            ExecutionPhase.CHECK: ExecutionPhase.CHECKABILITY,
            ExecutionPhase.PRE_RUN_CHECK: ExecutionPhase.PRE_RUN_CHECKABILITY,
            ExecutionPhase.POST_RUN_CHECK: ExecutionPhase.POST_RUN_CHECKABILITY,
        }[self]

    def __str__(self):
        return self.value


@dataclass
class PhaseSpan:
    phase: ExecutionPhase
    started_at: datetime
    completed_at: datetime | None = None

    @property
    def duration(self) -> float | None:
        if self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None


@dataclass(eq=False)
class ExecutionRecord:
    # A value snapshot, never the task: the record outlives the session. The
    # batch-completion sweep replaces the registration stub (see TaskInfo).
    info: "TaskInfo"
    store: "Optional[ExecutionRecordStore]" = field(default=None, repr=False)
    logs: collections.deque = field(
        default_factory=lambda: collections.deque(
            maxlen=EXECUTION_RECORD_LOG_BUFFER_SIZE
        )
    )
    # PhaseSpan items in execution order. The same phase can occur more than one
    # time, for example when a later group probes a dependency again.
    phases: list = field(default_factory=list)
    # ExecutionPhase -> {transient_property name: safe str | NOT_MATERIALIZED}.
    # Captured per phase while the task runs (see InteractiveRunner.task_scope).
    transient_snapshots: dict = field(default_factory=dict)
    # ExecutionPhase -> tuple[CacheReadSnapshot]. A repeated phase overwrites,
    # so the last occurrence wins, exactly like transient_snapshots.
    cache_snapshots: dict = field(default_factory=dict)

    def __post_init__(self):
        # append_log runs on worker threads while log_tail snapshots on the
        # serve loop; an unguarded list(deque) raises mid-append.
        self._log_lock = threading.Lock()

    def __hash__(self):
        return hash(self.info.key)

    def __eq__(self, other):
        if not isinstance(other, ExecutionRecord):
            return NotImplemented
        return self.info.key == other.info.key

    @property
    def last_log(self) -> str:
        return self.logs[-1] if self.logs else ""

    @property
    def started_at(self) -> datetime | None:
        return self.phases[0].started_at if self.phases else None

    @property
    def completed_at(self) -> datetime | None:
        return self.phases[-1].completed_at if self.phases else None

    @property
    def duration(self) -> float | None:
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None

    @contextmanager
    def track_phase(self, phase):
        span = PhaseSpan(phase, datetime.now())
        self.phases.append(span)
        try:
            yield span
        finally:
            span.completed_at = datetime.now()

    def append_log(self, line: str):
        with self._log_lock:
            self.logs.append(line)

    def log_tail(self, limit):
        """The last `limit` log lines, as a consistent copy."""
        with self._log_lock:
            return list(self.logs)[-limit:]

    def notify_display_log(self, line: str):
        if self.store:
            self.store.emit_log_appended(self.info.key, line)


@dataclass
class ExecutionBatch:
    uuid: str
    action: ExecutionAction
    created_at: datetime
    task_count: int = 0
    status: ExecutionStatus = ExecutionStatus.QUEUED
    # Not read yet. It equals created_at while a batch starts synchronously. It
    # becomes the measure of the queue wait when the runner does the dispatch.
    started_at: datetime | None = None
    completed_at: datetime | None = None
    execution_context: Optional["TaskExecutionContext"] = None
    # Task identity keys, batch-scoped. The durable data stays in the
    # TaskStatus values, which fill this set again each batch.
    errored: set = field(default_factory=set)

    def __post_init__(self):
        self._stop_event = threading.Event()
        self._worker = None
        self._error = None
        # Serializes the status transitions: request_stop runs on an action
        # thread while start and complete run on the worker.
        self._transition_lock = threading.Lock()

    def attach_worker(self, thread):
        self._worker = thread

    def record_error(self, exc):
        self._error = exc

    @property
    def error(self):
        """The message of a recorded framework error, or None."""
        return str(self._error) if self._error is not None else None

    def release_error(self):
        """Replace a recorded error at session end with a type-and-message copy:
        frames, args and attributes such as AttributeError.obj retain tasks."""
        if self._error is not None:
            self._error = _detached_error(self._error)

    def wait(self, timeout=None):
        """Block until the worker of the batch finishes. The blocking runner APIs
        (bulk_run, headless run and check) submit and then wait. An exception
        that the body propagated (reraise_errors) is raised again here, on the
        thread of the submitter. Those APIs thus keep their abort behavior."""
        if self._worker is not None:
            self._worker.join(timeout)
        if self._error is not None:
            raise self._error

    @property
    def is_bulk(self):
        return self.task_count > 1

    @property
    def stop_requested(self):
        return self._stop_event.is_set()

    def request_stop(self):
        with self._transition_lock:
            if self.status not in (ExecutionStatus.QUEUED, ExecutionStatus.RUNNING):
                return
            self._stop_event.set()
            self.status = ExecutionStatus.STOPPED

    def start(self):
        with self._transition_lock:
            self.status = ExecutionStatus.RUNNING
            self.started_at = datetime.now()

    def complete(self):
        with self._transition_lock:
            if self.status != ExecutionStatus.STOPPED:
                self.status = (
                    ExecutionStatus.ERRORED
                    if self._error is not None
                    else ExecutionStatus.FINISHED
                )
            self.completed_at = datetime.now()


def _detached_error(exc):
    """A new exception that keeps only the type and the message of exc. A
    constructor that refuses one string argument gets a RuntimeError."""
    try:
        return type(exc)(str(exc))
    except Exception:
        return RuntimeError(f"{type(exc).__name__}: {exc}")


def new_batch(action, tasks):
    return ExecutionBatch(
        uuid=str(_uuid.uuid4()),
        action=action,
        created_at=datetime.now(),
        task_count=len(tasks),
    )
