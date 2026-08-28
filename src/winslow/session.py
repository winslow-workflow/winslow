import logging
import threading
import time
from contextlib import contextmanager
from enum import Enum

from winslow.actions import ActionHandler
from winslow.exceptions import SessionEndingError
from winslow.logger import SessionLogBuffer, release_session_logging, run_logger_name
from winslow.task.status import PROBLEMATIC_STATUSES, PASSING_STATUSES
from winslow.telemetry import emit_unscoped_error
from winslow.task.context import LogContext, scoped_log_context
from winslow.task.info import release_session_caches
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
        # The inbound half of the session boundary: every presentation layer
        # submits its actions here (see ActionHandler).
        self.actions = ActionHandler(self)
        # A log backlog a caller attached before any subscriber existed (see
        # SessionLogBuffer). None unless something sets it.
        self.log_buffer = None
        # The workflow exists before its session, so the session connects itself
        # here. The runner reads the logging identity of this run through this
        # link (see runner.task_scope and ContextStampFilter). Persistence also
        # attaches on the workflow, after this link (see Workflow.init_state).
        workflow._session = self

    @contextmanager
    def log_scope(self):
        """Run a block under the log context of this session. An emission from
        the block, for example a cache log, routes to the session logger and
        carries the session labels (see cache_logger, ContextStampFilter)."""
        context = LogContext(
            session_id=self.session_id,
            workflow_name=self.workflow.instance_name,
            workflow_instance=str(self.workflow),
            task_name=None,
            task_instance=None,
            batch_uuid=None,
        )
        with scoped_log_context(context):
            yield

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
        """Mark the session as ended and release the store. Call this only
        under the lifecycle lock. end and finalize_if_drained guarantee that
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
        release_session_caches()
        release_session_logging(self.session_id)
        # An ended session is not a restore candidate.
        self.workflow.archive_state()

    def mark_error(self, exc=None):
        """Mark the session as failed after an init error. This freezes its
        elapsed time. The caller passes the exception when it has one, so
        the telemetry hook can report it (see telemetry.py)."""
        self.status = SessionStatus.ERROR
        if self.ended_at is None:
            self.ended_at = time.time()
        # An errored session never becomes usable, so it is not a restore
        # candidate either.
        self.workflow.archive_state()
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


def _refuse_value(name, value, option):
    if option.choices and str(value) not in [str(c) for c in option.choices]:
        raise ValueError(
            f"{value!r} is not a choice of {name} - the choices are "
            f"{[str(c) for c in option.choices]}."
        )


def validate_values(workflow_name, workflow_kls, orchestrator, values, overrides):
    """Refuse a bad create payload with direction, before any initialization
    work runs. The descriptors name every option this checks against."""
    known_values = workflow_kls.config_meta
    known_overrides = orchestrator.config_meta
    for name in values:
        if name not in known_values:
            raise ValueError(
                f"{name!r} names no option of {workflow_name} - the options "
                f"are {sorted(known_values)}."
            )
        _refuse_value(name, values[name], known_values[name])
    for name in overrides:
        if name not in known_overrides:
            raise ValueError(
                f"{name!r} names no orchestrator override - the overrides "
                f"are {sorted(known_overrides)}."
            )
        _refuse_value(name, overrides[name], known_overrides[name])
    missing = [
        name
        for name, option in known_values.items()
        if option.required and option.default is None and values.get(name) is None
    ]
    if missing:
        raise ValueError(
            f"{workflow_name} requires {', '.join(missing)} - descriptors "
            f"names the options of the workflow."
        )


def create_session(
    orchestrator,
    state_store,
    registry,
    workflow_name,
    orchestrator_overrides=None,
    workflow_values=None,
    session_id=None,
    seed=False,
    origin="serve",
):
    """Build, initialize, persist, and register one session: the shared flow
    behind the serve create_session request and the local AppClient. origin
    stamps the manifest with the door that created the session. Raises with
    a directional message on an unknown workflow; a failure after
    registration marks the session errored and unregisters it.

    session_id and seed serve a restore: the caller passes the id of the
    stored manifest, so the session rebuilds under it, and seed=True replays
    the stored snapshots onto the store after the eligibility pass (see
    Workflow.seed_from_state)."""
    try:
        workflow_kls = orchestrator.workflow_registry[workflow_name]
    except KeyError:
        raise KeyError(
            f"workflow {workflow_name!r} names no collected workflow. "
            f"The workflows are {orchestrator.workflow_registry.names}."
        ) from None

    orchestrator_overrides = orchestrator_overrides or {}
    workflow_values = workflow_values or {}
    validate_values(
        workflow_name,
        workflow_kls,
        orchestrator,
        workflow_values,
        orchestrator_overrides,
    )
    session_id = session_id or generate_id(workflow_name)
    workflow_logger = logging.getLogger(run_logger_name(session_id))
    workflow_logger.propagate = True
    # Attached before any initialization work runs, so init and eligibility
    # lines survive until a client subscribes (see SessionLogBuffer).
    log_buffer = SessionLogBuffer()
    workflow_logger.addHandler(log_buffer)

    init_log_ctx = LogContext(
        session_id=session_id,
        workflow_name=workflow_name,
        workflow_instance=workflow_name,
        task_name=None,
        task_instance=None,
        batch_uuid=None,
    )
    with scoped_log_context(init_log_ctx):
        workflow = orchestrator.initialize_workflow(
            workflow_kls=workflow_kls,
            orchestrator_overrides=orchestrator_overrides,
            workflow_values=workflow_values,
            logger=workflow_logger,
        )
        session = Session(workflow, session_id=session_id)
        session.log_buffer = log_buffer
        registry.register(session)
        try:
            workflow.initialize_tasks(logger=workflow.logger)
            workflow.check_pipeline_eligibility(logger=workflow.logger)
            # Persistence starts only once the pipeline is runnable: a kill
            # during the initialization above leaves no restore candidate.
            workflow.init_state(
                state_store,
                origin=origin,
                orchestrator_overrides=orchestrator_overrides,
                workflow_values=workflow_values,
            )
            if seed:
                # After the eligibility pass: that pass overwrites earlier
                # status writes (see Workflow.seed_from_state).
                workflow.seed_from_state()
        except Exception as exc:
            registry.remove(session_id)
            session.mark_error(exc)
            raise
    return session


class SessionRegistry:
    """The live sessions of one process, by session id. One registry serves
    every consumer of the process: the TUI app, and the serve transports
    (websocket, MCP), so each resolves the same map."""

    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()

    def register(self, session):
        with self._lock:
            self._sessions[session.session_id] = session

    def resolve(self, session_id):
        """The live session under the id. Raises KeyError with direction."""
        session = self.get(session_id)
        if session is None:
            raise KeyError(
                f"session id {session_id!r} does not resolve to a live session - "
                f"it ended, or it belongs to another process."
            )
        return session

    def get(self, session_id):
        return self._sessions.get(session_id)

    def remove(self, session_id):
        """Drop and return the session, or None: a teardown can run twice."""
        with self._lock:
            return self._sessions.pop(session_id, None)

    def sessions(self):
        # A tuple, so iteration survives a registration from another thread.
        return tuple(self._sessions.values())

    def __contains__(self, session_id):
        return session_id in self._sessions

    def __len__(self):
        return len(self._sessions)
