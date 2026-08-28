import collections
import copy
import json
import queue
import logging
import sys
import threading
import time

from contextlib import contextmanager
from pathlib import Path
from logging.handlers import QueueHandler, QueueListener

from winslow.settings import config
from winslow.util import safe_repr
from rich.logging import RichHandler


LOGGER = logging.getLogger("winslow")

# The parent logger of each run log and task log. A producer logs under
# winslow.runs.*. The sink and the propagate=False boundary are here, and
# setup_run_logging installs them. A task log thus reaches the sinks, but never
# the console handler of the root logger.
RUNS_LOGGER_NAME = "winslow.runs"

# The attribute on a control record that tells a sink to free the resources of a
# session that ended. A sink must test it before it reads the record as data.
RELEASE_MARKER = "winslow_release_session"

# The attribute values that a buffered record can carry as they are. Any other
# value, for example an object in `extra`, becomes its safe repr (see _sanitize).
_PLAIN_ATTRS = (str, int, float, bool, type(None))


def run_logger_name(session_id):
    return f"{RUNS_LOGGER_NAME}.workflow.{session_id}"


# All tasks share this one logger. A record carries the id of its task, which the
# LoggerAdapter of that task stamps on it. The TaskLogDispatcher can thus route
# each record back to its task.
TASK_LOGGER_NAME = f"{RUNS_LOGGER_NAME}.task"

LOG_FORMAT = config(
    "WINSLOW_LOG_FORMAT", default="%(asctime)s - %(levelname)s - %(message)s"
)
LOG_DATEFMT = config("WINSLOW_LOG_DATEFMT", default="%Y-%m-%d %H:%M:%S UTC")
LOG_UTC = config("WINSLOW_LOG_UTC", default=True, cast=bool)

INTERACTIVE_FORMATTER = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)
if LOG_UTC:
    INTERACTIVE_FORMATTER.converter = time.gmtime

INLINE_FORMATTER = logging.Formatter("%(levelname)s - %(message)s")


def initialize_logging(debug_mode: bool):
    """Initialize the logging for the given debug mode."""
    logging_level = logging.DEBUG if debug_mode else logging.INFO

    if sys.stdout.isatty():
        formatter = logging.Formatter("%(asctime)s  %(message)s", datefmt=LOG_DATEFMT)
        if LOG_UTC:
            formatter.converter = time.gmtime
        handler = RichHandler(rich_tracebacks=True, show_time=False)
        handler.setFormatter(formatter)
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(INTERACTIVE_FORMATTER)

    logging.basicConfig(
        level=logging_level,
        handlers=[handler],
    )

    LOGGER.setLevel(logging_level)


class InteractiveLogHandler(RichHandler):
    def __init__(self, log_method=None, formatter=None):
        logging.Handler.__init__(self)
        self.log_method = log_method
        self.formatter = formatter or INTERACTIVE_FORMATTER

    def emit(self, record):
        if self.log_method:
            self.log_method(self.format(record))


class SessionLogBuffer(logging.Handler):
    """A bounded backlog of one session's log lines. Attach at session
    creation, not at first subscribe: init and eligibility lines happen
    before any client can know the session id to subscribe with, and this
    handler catches them anyway. TaskLogDispatcher.buffered() is the same
    idea for one task."""

    def __init__(self, maxlen=200):
        super().__init__()
        self.setFormatter(INLINE_FORMATTER)
        self.lines = collections.deque(maxlen=maxlen)

    def emit(self, record):
        self.lines.append(self.format(record))


class ContextStampFilter(logging.Filter):
    """A filter that adds data. It stamps the labels of the current LogContext
    onto each record, so a sink gets structured metadata. It never drops a record
    and always returns True. Each field is None outside the scope of a task, for
    example during the init or the orchestration."""

    FIELDS = (
        "session_id",
        "workflow_name",
        "workflow_instance",
        "task_name",
        "task_instance",
        "batch_uuid",
    )

    def filter(self, record):
        # A lazy import. winslow.task.context and winslow.logger would otherwise
        # make an import cycle. decorators.py imports it lazily for the same
        # reason.
        from winslow.task.context import get_log_context

        ctx = get_log_context()
        for field in self.FIELDS:
            setattr(record, field, getattr(ctx, field, None) if ctx else None)
        return True


class TaskLogDispatcher(logging.Handler):
    """The one handler on the shared task logger (TASK_LOGGER_NAME). It sends each
    record to the task that it belongs to. The key is the task_id that the
    LoggerAdapter of the task stamps on the record.

    The dispatcher holds the log buffer of each interactive task, which holds raw
    records for the info modal, and also the live listeners, which are the
    handlers of an execution record and an open modal. The key is the task_id. A
    buffer lives longer than one execution, and it is dropped when the garbage
    collector frees the task (see Task.__init__). A listener is added and removed
    around the scope that needs it."""

    def __init__(self):
        super().__init__()
        self._buffers = {}  # task_id -> deque[LogRecord]
        self._listeners = {}  # task_id -> list[logging.Handler]
        self._lock = threading.RLock()

    def register_buffer(self, task_id, buffer):
        with self._lock:
            self._buffers[task_id] = buffer

    def unregister(self, task_id):
        with self._lock:
            self._buffers.pop(task_id, None)
            self._listeners.pop(task_id, None)

    def buffered(self, task_id):
        """The buffered records of a task. A log view reads the backlog here
        and then subscribes by the same uuid, so it never touches the task."""
        with self._lock:
            buffer = self._buffers.get(task_id)
            return tuple(buffer) if buffer is not None else ()

    def add_listener(self, task_id, handler):
        with self._lock:
            self._listeners.setdefault(task_id, []).append(handler)

    def remove_listener(self, task_id, handler):
        with self._lock:
            listeners = self._listeners.get(task_id)
            if listeners and handler in listeners:
                listeners.remove(handler)

    @contextmanager
    def listen(self, task_id, *handlers):
        for handler in handlers:
            self.add_listener(task_id, handler)
        try:
            yield
        finally:
            for handler in handlers:
                self.remove_listener(task_id, handler)

    @classmethod
    def _sanitize(cls, record):
        """Render a record to text before it is buffered. The buffer outlives
        the execution, so an exception or an extra object retains the task."""
        # not record.args, because a no-arg call carries (), never None. msg,
        # args and exc_info have their own tests, so the attribute scan skips them.
        plain = (
            not record.args
            and record.exc_info is None
            and isinstance(record.msg, str)
            and all(
                isinstance(v, _PLAIN_ATTRS)
                for k, v in vars(record).items()
                if k not in ("msg", "args", "exc_info")
            )
        )
        if plain:
            return record
        record = copy.copy(record)
        record.msg = record.getMessage()
        record.args = None
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
        vars(record).update(
            {
                name: safe_repr(value)
                for name, value in vars(record).items()
                if not isinstance(value, _PLAIN_ATTRS)
            }
        )
        return record

    def emit(self, record):
        task_id = getattr(record, "task_id", None)
        if task_id is None:
            return
        with self._lock:
            buffer = self._buffers.get(task_id)
            listeners = list(self._listeners.get(task_id, ()))
        if buffer is None and not listeners:
            return
        record = self._sanitize(record)
        if buffer is not None:
            buffer.append(record)
        for handler in listeners:
            handler.handle(record)


_task_dispatcher = None
_task_dispatcher_lock = threading.Lock()


def get_task_dispatcher():
    """The process-wide dispatcher of the task logs. It attaches to the shared
    task logger at the first use. This function is idempotent."""
    global _task_dispatcher
    if _task_dispatcher is None:
        with _task_dispatcher_lock:
            if _task_dispatcher is None:
                dispatcher = TaskLogDispatcher()
                logger = logging.getLogger(TASK_LOGGER_NAME)
                logger.setLevel(LOGGER.level)
                logger.addHandler(dispatcher)
                _task_dispatcher = dispatcher
    return _task_dispatcher


class StructuredFormatter(logging.Formatter):
    """Render a stamped record as one JSON object. This is the shared shape. The
    file sink reads it now, and a websocket sink can read it later. The bytes to
    the disk and to the network are thus the same."""

    def format(self, record):
        return json.dumps(
            {
                "ts": self.formatTime(record),
                "level": record.levelname,
                "session_id": getattr(record, "session_id", None),
                "workflow_name": getattr(record, "workflow_name", None),
                "workflow_instance": getattr(record, "workflow_instance", None),
                "task_name": getattr(record, "task_name", None),
                "task_instance": getattr(record, "task_instance", None),
                "batch_uuid": getattr(record, "batch_uuid", None),
                "message": record.getMessage(),
            }
        )


class SessionFileSink(logging.Handler):
    """Send the records to one JSONL file per session
    (<log_dir>/<session_id>.jsonl). This is a copy of a per-session websocket or
    Grafana stream. A stdlib FileHandler controls the file of each session: the
    lazy open, the flush at each emit, the encoding and the clean close. This
    class thus only routes by the stamped session_id. It runs on the listener
    thread, so the file I/O is not on the hot path of a task. A file name is
    sanitized at this boundary. The session_id in the JSON does not change."""

    def __init__(self, log_dir):
        super().__init__()
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._formatter = StructuredFormatter()
        self._handlers = {}  # sanitized session_id -> FileHandler

    def _handler_for(self, session_id):
        # session_id is safe as a file name by construction. It is a validated
        # workflow name ([A-Za-z0-9_-]+, see _Base.get_name) with a timestamp and
        # a uuid4 suffix. A per-task file, if one is added later, takes its name
        # from the task parameters, which are arbitrary and not validated. Such a
        # name WILL need a sanitize step here.
        key = session_id or "_unscoped"
        handler = self._handlers.get(key)
        if handler is None:
            handler = logging.FileHandler(
                self.log_dir / f"{key}.jsonl", encoding="utf-8", delay=True
            )
            handler.setFormatter(self._formatter)
            self._handlers[key] = handler
        return handler

    def emit(self, record):
        released = getattr(record, RELEASE_MARKER, None)
        if released is not None:
            self._release(released)
            return
        self._handler_for(getattr(record, "session_id", None)).emit(record)

    def _release(self, session_id):
        # This runs on the listener thread, as each other access to _handlers.
        # The close thus cannot race an emit that is in progress.
        handler = self._handlers.pop(session_id or "_unscoped", None)
        if handler is not None:
            handler.close()

    def close(self):
        for handler in self._handlers.values():
            handler.close()
        self._handlers.clear()
        super().close()


# The common transport boundary. Each producer, which is a logger under
# winslow.runs.*, writes to one QueueHandler. A QueueListener then drains the
# queue on a background thread to the registered sinks: the file now, and a
# websocket later. See setup_run_logging.
_log_queue = None
_listener = None


def _default_sinks():
    return [SessionFileSink(config("WINSLOW_LOG_DIR", default=".winslow/logs"))]


def setup_run_logging(sinks=None):
    """Build the common run-logging boundary. This function is idempotent.

        winslow.runs (propagate=False)
            └─ QueueHandler [+ ContextStampFilter]  ──▶ queue
                                                          └─▶ QueueListener (bg) ─▶ sinks

    A record is stamped on the side of the producer, because the filter at handler
    level also runs for a record that a child logger propagates. The queue then
    drains off the hot path. Call this BEFORE a task logger or a workflow logger
    starts to propagate. If you do not, their logs go to the console until the
    propagate=False boundary exists. To add a websocket sink later, pass it in
    `sinks`."""
    global _log_queue, _listener
    if _listener is not None:
        return

    runs_logger = logging.getLogger(RUNS_LOGGER_NAME)
    runs_logger.propagate = False
    runs_logger.setLevel(LOGGER.level)

    _log_queue = queue.Queue(-1)
    qh = QueueHandler(_log_queue)
    qh.addFilter(ContextStampFilter())
    runs_logger.addHandler(qh)

    _listener = QueueListener(
        _log_queue, *(sinks or _default_sinks()), respect_handler_level=True
    )
    _listener.start()


def shutdown_run_logging():
    global _listener
    if _listener is not None:
        _listener.stop()
        _listener = None


def release_session_logging(session_id):
    """Free the logging resources of a session that ended. The release goes
    through the log queue as a control record. The FIFO order guarantees that each
    record before this call is written first, so no record is lost and the caller
    does not block. The named logger is also removed from the registry, because a
    named logger otherwise lives for the life of the process."""
    logging.Logger.manager.loggerDict.pop(run_logger_name(session_id), None)
    if _log_queue is None:
        return
    record = logging.makeLogRecord(
        {RELEASE_MARKER: session_id, "levelno": logging.CRITICAL}
    )
    _log_queue.put_nowait(record)
