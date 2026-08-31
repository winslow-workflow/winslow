"""The wire vocabulary shared by the serve transports: the action frame
builder and the row shapes of sessions, descriptors, caches, and history.
Both the websocket layer and the MCP tools read these."""

from dataclasses import asdict, is_dataclass

from winslow.exceptions import MisconfigurationError, RequestError

from winslow.actions import (
    CheckTasks,
    ClearCacheEntries,
    EndSession,
    LoadCacheEntries,
    RunTasks,
    StopBatch,
)
from winslow.model import (
    ApplyFilterRequest,
    BatchOptionsRequest,
    CachesRequest,
    CacheValueRequest,
    CreateSessionRequest,
    Descriptors,
    DescriptorsRequest,
    HistoryRequest,
    LogTailRequest,
    ManifestsRequest,
    RecordDetailRequest,
    RestoreSessionRequest,
    RosterRequest,
    SessionParamsRequest,
    SessionRow,
    SessionsRequest,
    SnapshotRequest,
    TaskDetailRequest,
)


class FrameTypes:
    """The top-level "type" values of a frame: the six a client sends (see
    Connection.handle_frame), and every one the server sends back, over the
    control channel (see Connection.reply) or a session subscription (see
    EventBridge._fan_out). hello and hello_ok belong to the handshake,
    before a Connection exists."""

    # Client to server.
    HELLO = "hello"
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"
    SUBSCRIBE_TASK_LOG = "subscribe_task_log"
    UNSUBSCRIBE_TASK_LOG = "unsubscribe_task_log"
    ACTION = "action"
    REQUEST = "request"

    # Server to client: control replies.
    HELLO_OK = "hello_ok"
    HELLO_ERROR = "hello_error"
    ERROR = "error"
    RESULT = "result"
    ACK = "ack"
    UNSUBSCRIBED = "unsubscribed"
    UNSUBSCRIBED_TASK_LOG = "unsubscribed_task_log"
    TASK_LOG_BACKLOG = "task_log_backlog"

    # Server to client: the session subscription (snapshot, then events).
    SNAPSHOT = "snapshot"
    TASK_STATUS = "task_status"
    EXECUTION_STATUS = "execution_status"
    BATCH_CREATED = "batch_created"
    BATCH_COMPLETED = "batch_completed"
    SESSION_ENDED = "session_ended"
    CACHE_UPDATED = "cache_updated"
    LOG_BATCH = "log_batch"
    SESSION_LOG_BATCH = "session_log_batch"
    TASK_LOG_BATCH = "task_log_batch"


# The frame types a client may send. The unknown-type error message lists
# these, so the two cannot drift apart (see Connection.handle_frame).
INBOUND_FRAME_TYPES = (
    FrameTypes.SUBSCRIBE,
    FrameTypes.UNSUBSCRIBE,
    FrameTypes.SUBSCRIBE_TASK_LOG,
    FrameTypes.UNSUBSCRIBE_TASK_LOG,
    FrameTypes.ACTION,
    FrameTypes.REQUEST,
)


class Actions:
    """The action names of the wire protocol (see ACTION_CLASSES). A frame
    names one of these under "action"."""

    RUN_TASKS = "run_tasks"
    CHECK_TASKS = "check_tasks"
    STOP_BATCH = "stop_batch"
    END_SESSION = "end_session"
    LOAD_CACHE_ENTRIES = "load_cache_entries"
    CLEAR_CACHE_ENTRIES = "clear_cache_entries"


class Requests:
    """The request kinds of the wire protocol (see Connection.run_request).
    A frame names one of these under "kind"."""

    CREATE_SESSION = "create_session"
    DESCRIPTORS = "descriptors"
    SESSIONS = "sessions"
    SNAPSHOT = "snapshot"
    HISTORY = "history"
    LOG_TAIL = "log_tail"
    TASK_DETAIL = "task_detail"
    ROSTER = "roster"
    CACHES = "caches"
    CACHE_VALUE = "cache_value"
    RECORD_DETAIL = "record_detail"
    BATCH_OPTIONS = "batch_options"
    SESSION_PARAMS = "session_params"
    APPLY_FILTER = "apply_filter"
    MANIFESTS = "manifests"
    RESTORE_SESSION = "restore_session"


# The frame names the action, the fields fill the dataclass (see
# winslow.actions).
ACTION_CLASSES = {
    Actions.RUN_TASKS: RunTasks,
    Actions.CHECK_TASKS: CheckTasks,
    Actions.STOP_BATCH: StopBatch,
    Actions.END_SESSION: EndSession,
    Actions.LOAD_CACHE_ENTRIES: LoadCacheEntries,
    Actions.CLEAR_CACHE_ENTRIES: ClearCacheEntries,
}


# The frame names the request kind, the class validates the fields that kind
# needs (see Connection.dispatch_request).
REQUEST_CLASSES = {
    Requests.CREATE_SESSION: CreateSessionRequest,
    Requests.DESCRIPTORS: DescriptorsRequest,
    Requests.SESSIONS: SessionsRequest,
    Requests.SNAPSHOT: SnapshotRequest,
    Requests.HISTORY: HistoryRequest,
    Requests.LOG_TAIL: LogTailRequest,
    Requests.TASK_DETAIL: TaskDetailRequest,
    Requests.ROSTER: RosterRequest,
    Requests.CACHES: CachesRequest,
    Requests.CACHE_VALUE: CacheValueRequest,
    Requests.RECORD_DETAIL: RecordDetailRequest,
    Requests.BATCH_OPTIONS: BatchOptionsRequest,
    Requests.SESSION_PARAMS: SessionParamsRequest,
    Requests.APPLY_FILTER: ApplyFilterRequest,
    Requests.MANIFESTS: ManifestsRequest,
    Requests.RESTORE_SESSION: RestoreSessionRequest,
}


def session_row(session):
    return asdict(SessionRow.from_session(session))


# The refusals of a port read: an unknown session id, a served refusal, a
# missing orchestrator or store. Each door maps them to its error shape.
READ_REFUSALS = (KeyError, RequestError, MisconfigurationError)


def refusal_reason(exc):
    return str(exc.args[0] if exc.args else exc)


def result_payload(value):
    """The wire form of one port read result: a dataclass becomes a dict, a
    tuple of dataclasses a list, a scalar passes through."""
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, (tuple, list)):
        return [result_payload(item) for item in value]
    return value


def build_action(name, fields):
    """The action dataclass for one wire frame. Raises ValueError with a
    directional message on an unknown name or on bad fields."""
    action_class = ACTION_CLASSES.get(name)
    if action_class is None:
        raise ValueError(
            f"{name!r} names no action. The actions are {sorted(ACTION_CLASSES)}."
        )
    fields = dict(fields or {})
    if "keys" in fields:
        fields["keys"] = tuple(fields["keys"])
    if "entries" in fields:
        fields["entries"] = tuple(tuple(pair) for pair in fields["entries"])
    try:
        return action_class(**fields)
    except TypeError as exc:
        raise ValueError(f"bad fields for {name}: {exc}") from None


def descriptor_rows(orchestrator):
    return asdict(Descriptors.from_orchestrator(orchestrator))
