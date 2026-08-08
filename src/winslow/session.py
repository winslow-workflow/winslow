import threading
import time
from contextlib import contextmanager
from enum import Enum

from winslow.exceptions import SessionEndingError
from winslow.logger import release_session_logging
from winslow.telemetry import emit_unscoped_error
from winslow.task.status import PROBLEMATIC_STATUSES, PASSING_STATUSES
from winslow.util import generate_id


class SessionStatus(Enum):
    ACTIVE = "active"
    ENDING = "ending"
    ENDED = "ended"
    ERROR = "error"


class Session:
    def __init__(self, workflow, session_id=None):
        self.workflow = workflow
        self.status = SessionStatus.ACTIVE
        # A readable identity for this execution session, ordered by time. The
        # caller can supply it, so one id can name the workflow logger first and
        # also identify the Session. If the caller supplies none, the session
        # generates its own.
        self.session_id = session_id or generate_id(workflow.instance_name)
        self.start = time.time()
        # Set when the session ends. It freezes elapsed and marks the session as
        # history.
        self.ended_at = None
        # A snapshot of task_status_summary, taken at the end. The store is
        # cleared at that moment.
        self._final_summary = None
        # This serializes the batch admission against the end of the session. No
        # batch can start in a session that is ending, and no end can occur
        # between the admission check of a batch and its registration.
        self._lifecycle_lock = threading.Lock()
        # The workflow exists before its session, so the session connects itself
        # here. The runner reads the logging identity of this run through this
        # link (see runner.task_scope and ContextStampFilter).
        workflow._session = self

    def __str__(self):
        return self.session_id

    def __repr__(self):
        return f"< Session '{self.session_id}' >"

    @property
    def workflow_name(self):
        return self.workflow.instance_name

    @property
    def has_ended(self):
        return self.ended_at is not None

    @property
    def is_ending(self):
        return self.status is SessionStatus.ENDING

    @property
    def elapsed(self):
        """The number of seconds that the session has run. It freezes when the
        session ends."""
        end = self.ended_at if self.ended_at is not None else time.time()
        return end - self.start

    @property
    def active_batches(self):
        return self.workflow.runner.active_batches

    @contextmanager
    def batch_admission(self):
        """The runner registers a new batch in this scope. The refusal and the
        registration are atomic against end(). A batch is thus registered before
        the end decision, which then waits for the batch, or the batch is refused
        with an error."""
        with self._lifecycle_lock:
            if self.is_ending or self.has_ended:
                raise SessionEndingError(f"{self.session_id} no longer accepts batches")
            yield

    def end(self):
        """A clear of the store during a run would race the batch threads that
        read it. The session thus goes to ENDING while the batches run.
        finalize_if_drained completes the end when they drain."""
        with self._lifecycle_lock:
            if self.has_ended or self.is_ending:
                return
            self.status = SessionStatus.ENDING
            if self.active_batches:
                return
            self._finalize_end()

    def force_end(self):
        """Stop the batches that run. They then drain into finalize_if_drained.
        The session ends first, so a batch that is admitted at the same time
        cannot escape the sweep."""
        self.end()
        for batch in self.active_batches:
            batch.request_stop()

    def finalize_if_drained(self):
        """The rule from ENDING to ENDED, in one place: a session that is ending
        finalizes when its last batch completes. The callers, which are the
        lifecycle adapter of the app and the tests, call this when a batch
        completes."""
        with self._lifecycle_lock:
            if self.is_ending and not self.active_batches:
                self._finalize_end()

    def _finalize_end(self):
        """Mark the session as ended and release the store. Call this only while
        you hold the lifecycle lock. end and finalize_if_drained guarantee that
        no batch runs and that no batch can be admitted."""
        if self.has_ended:
            return
        self.ended_at = time.time()
        self.status = SessionStatus.ENDED
        # Take the snapshot before the clear of the store removes the data.
        self._final_summary = self.task_status_summary
        # Release each task that execution history does not retain, so the
        # garbage collector can free it.
        self.workflow.release_tasks()
        release_session_logging(self.session_id)

    def mark_error(self, exc=None):
        """Mark the session as failed after an init error. This freezes its
        elapsed time. The caller passes the exception when it has one, so
        the telemetry hook can report it (see telemetry.py)."""
        self.status = SessionStatus.ERROR
        if self.ended_at is None:
            self.ended_at = time.time()
        if exc is not None:
            emit_unscoped_error(
                exc,
                workflow_name=self.workflow_name,
                workflow_instance=str(self.workflow),
                workflow_class=type(self.workflow).__name__,
                session_id=self.session_id,
            )

    @property
    def screen_name(self):
        return f"session-{self.session_id}"

    @property
    def task_status_summary(self):
        """Return the (completed, problematic, total) counts from the live task
        store."""
        if self._final_summary is not None:
            return self._final_summary
        statuses = list(self.workflow.store.values())
        completed = sum(1 for s in statuses if s in PASSING_STATUSES)
        problematic = sum(1 for s in statuses if s in PROBLEMATIC_STATUSES)
        return completed, problematic, len(statuses)
