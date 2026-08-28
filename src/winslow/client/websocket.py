"""The wire transport of the session port: the same client pair as
winslow.client.local, built from serve protocol frames. Reads travel as
request frames, actions as action frames, and the subscriptions of one
session ride one server-side subscription. The codec decodes every payload
back into the model dataclasses, so both transports hand a pane the same
values (the parity rule, see winslow.model).

One receiver thread reads every frame of the socket. On a dropped
connection it reconnects with backoff, replays the hello, and resubscribes
every session lane. On a sequence gap it resubscribes the gapped session:
the server then resets the queue and resends the snapshot (see
Connection.handle_subscribe), and the lane heals its subscribers from it.

This module needs the connect extra (websockets, pydantic). The local TUI
never imports it (see winslow.client)."""

import itertools
import json
import threading
import time
from dataclasses import asdict
from urllib.parse import urlsplit, urlunsplit

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect as websocket_connect

from winslow.actions import Ack, BatchAck, CheckTasks, RunTasks
from winslow.client.base import AppClient, SessionClient
from winslow.codec import CODEC
from winslow.events import (
    BatchCompletedEvent,
    BatchCreatedEvent,
    ExecutionStatusEvent,
    LogLineEvent,
    Origin,
    SessionEndedEvent,
    TaskStatusEvent,
)
from winslow.exceptions import MisconfigurationError
from winslow.logger import LOGGER
from winslow.model import (
    BatchInfo,
    CacheCard,
    CacheUpdatedEvent,
    CacheValueView,
    Descriptors,
    HistoryRow,
    ManifestRow,
    RecordDetail,
    SessionLogEvent,
    SessionParams,
    SessionRow,
    SessionSnapshot,
    TaskInfo,
    TaskLogEvent,
)
from winslow.serve.wire import ACTION_CLASSES, FrameTypes, Requests
from winslow.task.status import TaskStatus

# The action name of each action class: the reverse of the serve-side map,
# so the two sides cannot drift apart.
ACTION_NAMES = {action_class: name for name, action_class in ACTION_CLASSES.items()}

OPEN_TIMEOUT = 5.0
REQUEST_TIMEOUT = 60.0
RECONNECT_DELAY = 0.5
RECONNECT_DELAY_MAX = 5.0

CONNECTION_DOWN = (
    "the connection to the serve process is down - the client reconnects "
    "in the background; retry once it is back."
)


def normalize_url(url):
    """The websocket endpoint for a connect URL: ws://host:port resolves to
    the /ws route of the serve process; an explicit path stays."""
    parts = urlsplit(url)
    if parts.scheme not in ("ws", "wss"):
        raise MisconfigurationError(
            f"{url!r} is not a websocket URL - connect to ws://host:port "
            f"(or wss:// behind TLS)."
        )
    path = parts.path if parts.path not in ("", "/") else "/ws"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


class Pending:
    """One in-flight exchange: the caller blocks on wait, the receiver
    thread resolves with the answer frame."""

    def __init__(self):
        self._event = threading.Event()
        self.frame = None

    def resolve(self, frame):
        self.frame = frame
        self._event.set()

    def wait(self, timeout):
        if not self._event.wait(timeout):
            raise TimeoutError(
                f"no answer from the serve process within {timeout:g}s - "
                f"the server may be overloaded or unreachable."
            )
        return self.frame


class Wire:
    """One websocket to a serve process, shared by every client of the
    connection. Senders write from any thread (the sync websocket client
    locks its writes); the receiver thread reads every frame and routes it
    to a pending exchange or a session lane."""

    def __init__(self, url, token=None, ticket=None, open_timeout=OPEN_TIMEOUT):
        self.url = normalize_url(url)
        self.token = token
        self.ticket = ticket
        self.open_timeout = open_timeout
        self._websocket = None
        self._receiver = None
        self._closing = threading.Event()
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        # Pending exchanges: requests and acks by request id, task log
        # backlogs by (session id, task key) - that reply carries no id.
        self._pending = {}
        self._backlog_pending = {}
        # One shared lane per session id (see session_lane): the server
        # keeps one subscription per session per socket, so a second client
        # of the same session must not reset the first one's stream.
        self._lanes = {}

    def connect(self):
        try:
            self._websocket = self._open()
        except (OSError, TimeoutError, WebSocketException) as exc:
            raise MisconfigurationError(
                f"cannot connect to {self.url} - {exc}. Is the serve "
                f"process running there?"
            ) from exc
        self._receiver = threading.Thread(
            target=self._receive_loop, name="winslow-wire", daemon=True
        )
        self._receiver.start()

    def close(self):
        self._closing.set()
        self._fail_pending("the client is closed.")
        websocket = self._websocket
        if websocket is not None:
            websocket.close()
        if self._receiver is not None and self._receiver.is_alive():
            self._receiver.join(timeout=2.0)

    def next_id(self):
        return f"c-{next(self._ids)}"

    def session_lane(self, session_id):
        with self._lock:
            lane = self._lanes.get(session_id)
            if lane is None:
                lane = RemoteSessionClient(self, session_id)
                self._lanes[session_id] = lane
            return lane

    def drop_lane(self, session_id):
        with self._lock:
            self._lanes.pop(session_id, None)

    # --- the outgoing side -----------------------------------------------------

    def send(self, payload):
        try:
            self._websocket.send(json.dumps(payload))
        except (OSError, WebSocketException, AttributeError) as exc:
            raise ConnectionError(CONNECTION_DOWN) from exc

    def request(self, kind, timeout=REQUEST_TIMEOUT, **fields):
        """One request frame, blocking until its result. A server refusal
        raises ValueError with the served reason."""
        request_id = self.next_id()
        pending = Pending()
        self._pending[request_id] = pending
        try:
            self.send(
                {
                    "type": FrameTypes.REQUEST,
                    "kind": kind,
                    "request_id": request_id,
                    **fields,
                }
            )
            frame = pending.wait(timeout)
        finally:
            self._pending.pop(request_id, None)
        if frame["type"] == FrameTypes.ERROR:
            raise ValueError(self._error_text(frame))
        return frame

    @classmethod
    def _error_text(cls, frame):
        """The served reason, with the detail (a server traceback) behind
        it, so the create and restore error modal shows the real cause."""
        reason = frame.get("reason", CONNECTION_DOWN)
        detail = frame.get("detail")
        return f"{reason}\n\n{detail}" if detail else reason

    def action(self, session_id, action):
        """One action frame, blocking until its ack. A transport failure
        answers a refused ack, never a raise: the action contract of the
        handler (see ActionHandler)."""
        name = ACTION_NAMES.get(type(action))
        if name is None:
            return self._refused(
                action,
                f"{type(action).__name__} names no action of the wire - "
                f"the actions are {sorted(c.__name__ for c in ACTION_NAMES)}.",
            )
        request_id = self.next_id()
        pending = Pending()
        self._pending[request_id] = pending
        try:
            self.send(
                {
                    "type": FrameTypes.ACTION,
                    "session_id": session_id,
                    "action": name,
                    "request_id": request_id,
                    "fields": asdict(action),
                }
            )
            frame = pending.wait(REQUEST_TIMEOUT)
        except (ConnectionError, TimeoutError) as exc:
            return self._refused(action, str(exc))
        finally:
            self._pending.pop(request_id, None)
        if frame["type"] == FrameTypes.ERROR:
            return self._refused(action, self._error_text(frame))
        if "batch_uuid" in frame:
            return BatchAck(
                accepted=frame["accepted"],
                reason=frame["reason"],
                batch_uuid=frame["batch_uuid"],
            )
        return Ack(accepted=frame["accepted"], reason=frame["reason"])

    @classmethod
    def _refused(cls, action, reason):
        ack_class = BatchAck if isinstance(action, (RunTasks, CheckTasks)) else Ack
        return ack_class(accepted=False, reason=reason)

    def backlog_exchange(self, session_id, task_key, request_id):
        """Register the pending of one subscribe_task_log frame. The success
        reply correlates by (session id, task key), the refusal by request
        id, so the pending sits in both maps."""
        pending = Pending()
        self._pending[request_id] = pending
        self._backlog_pending[(session_id, task_key)] = pending
        return pending

    def drop_backlog_exchange(self, session_id, task_key, request_id):
        self._pending.pop(request_id, None)
        self._backlog_pending.pop((session_id, task_key), None)

    # --- the receiver thread -----------------------------------------------------

    def _open(self):
        """One handshaken socket: hello sent, hello_ok and the hello-time
        snapshot consumed. The sessions read re-fetches fresh rows, so the
        snapshot frame is only drained here."""
        websocket = websocket_connect(self.url, open_timeout=self.open_timeout)
        try:
            hello = {"type": FrameTypes.HELLO}
            if self.token:
                hello["token"] = self.token
            if self.ticket:
                hello["ticket"] = self.ticket
            websocket.send(json.dumps(hello))
            reply = json.loads(websocket.recv(timeout=self.open_timeout))
            if reply.get("type") != FrameTypes.HELLO_OK:
                raise MisconfigurationError(
                    f"{self.url} refused the connection - "
                    f"{reply.get('reason', 'no hello_ok answer')}"
                )
            json.loads(websocket.recv(timeout=self.open_timeout))
        except BaseException:
            websocket.close()
            raise
        return websocket

    def _receive_loop(self):
        while True:
            try:
                raw = self._websocket.recv()
            except (OSError, WebSocketException):
                if self._closing.is_set() or not self._reconnect():
                    return
                continue
            try:
                frame = json.loads(raw)
            except ValueError:
                continue
            if isinstance(frame, dict):
                self._route(frame)

    def _reconnect(self):
        """Reopen the socket with backoff, then resubscribe every lane. The
        callers of the outage read a refusal and retry; the resubscription
        snapshots heal the subscribers (see RemoteSessionClient.resubscribe)."""
        self._fail_pending(CONNECTION_DOWN)
        delay = RECONNECT_DELAY
        while not self._closing.is_set():
            try:
                self._websocket = self._open()
            except MisconfigurationError as exc:
                LOGGER.warning(f"the serve process refuses the reconnect: {exc}")
            except (OSError, TimeoutError, WebSocketException, ValueError):
                pass
            else:
                with self._lock:
                    lanes = tuple(self._lanes.values())
                for lane in lanes:
                    lane.resubscribe()
                return True
            time.sleep(delay)
            delay = min(delay * 2, RECONNECT_DELAY_MAX)
        return False

    def _fail_pending(self, reason):
        for pending in (*self._pending.values(), *self._backlog_pending.values()):
            pending.resolve({"type": FrameTypes.ERROR, "reason": reason})
        self._pending.clear()
        self._backlog_pending.clear()

    def _route(self, frame):
        kind = frame.get("type")
        if kind in (FrameTypes.RESULT, FrameTypes.ACK, FrameTypes.ERROR):
            pending = self._pending.get(frame.get("request_id"))
            if pending is not None:
                pending.resolve(frame)
            return
        if kind == FrameTypes.TASK_LOG_BACKLOG:
            key = (frame.get("session_id"), frame.get("task_key"))
            pending = self._backlog_pending.get(key)
            if pending is not None:
                pending.resolve(frame)
            return
        if kind in (FrameTypes.UNSUBSCRIBED, FrameTypes.UNSUBSCRIBED_TASK_LOG):
            return
        if kind == FrameTypes.SNAPSHOT and "session_id" not in frame:
            # The hello-time snapshot of a reconnect: superseded by the
            # sessions read.
            return
        lane = self._lanes.get(frame.get("session_id"))
        if lane is not None:
            lane.on_frame(frame)


class RemoteAppClient(AppClient):
    """The dashboard scope over one serve process. connect() opens the
    socket and must run before the first read. Reads the process cannot
    serve (no orchestrator, no state store) answer the served refusal as
    ValueError, mirroring the local MisconfigurationError direction."""

    def __init__(self, url, token=None, ticket=None, open_timeout=OPEN_TIMEOUT):
        self.wire = Wire(url, token=token, ticket=ticket, open_timeout=open_timeout)

    def connect(self):
        self.wire.connect()
        return self

    def close(self):
        self.wire.close()

    def sessions(self):
        frame = self.wire.request(Requests.SESSIONS)
        return tuple(CODEC.decode(SessionRow, row) for row in frame["sessions"])

    def descriptors(self):
        frame = self.wire.request(Requests.DESCRIPTORS)
        return CODEC.decode(Descriptors, _payload(frame))

    def manifests(self):
        frame = self.wire.request(Requests.MANIFESTS)
        return tuple(CODEC.decode(ManifestRow, row) for row in frame["manifests"])

    def create_session(self, workflow, overrides=None, values=None):
        frame = self.wire.request(
            Requests.CREATE_SESSION,
            workflow=workflow,
            overrides=overrides,
            values=values,
        )
        return CODEC.decode(SessionRow, _payload(frame))

    def restore_session(self, session_id):
        frame = self.wire.request(Requests.RESTORE_SESSION, session_id=session_id)
        return CODEC.decode(SessionRow, _payload(frame))

    def session(self, session_id):
        return self.wire.session_lane(session_id)


def _payload(frame):
    """The DTO fields of one result frame, without the envelope keys."""
    return {
        key: value
        for key, value in frame.items()
        if key not in ("type", "kind", "request_id")
    }


class RemoteSessionClient(SessionClient):
    """One session over the wire. The instance is also the session's lane:
    the wire routes every frame of the session here, and on_frame turns it
    into the model events the local adapter publishes. A handler runs on
    the receiver thread."""

    def __init__(self, wire, session_id):
        self.wire = wire
        self.session_id = session_id
        self._lock = threading.Lock()
        # {topic: [handler, ...]} and {task key: [handler, ...]}.
        self._handlers = {}
        self._task_log_handlers = {}
        # The server-side subscription state of this lane. last_seq stamps
        # the last dispatched frame; awaiting_snapshot drops event frames a
        # requested snapshot supersedes; healing marks a recovery, whose
        # snapshot re-emits the task statuses (see _on_snapshot).
        self._subscribed = False
        self._last_seq = None
        self._awaiting_snapshot = False
        self._healing = False

    # --- reads ---------------------------------------------------------------

    def _request(self, kind, **fields):
        return self.wire.request(kind, session_id=self.session_id, **fields)

    def snapshot(self):
        return CODEC.decode(SessionSnapshot, _payload(self._request(Requests.SNAPSHOT)))

    def roster(self):
        frame = self._request(Requests.ROSTER)
        return tuple(CODEC.decode(TaskInfo, row) for row in frame["tasks"])

    def task_detail(self, key):
        frame = self._request(Requests.TASK_DETAIL, task_key=key)
        return CODEC.decode(TaskInfo, frame["info"])

    def record_detail(self, batch_uuid, key):
        frame = self._request(
            Requests.RECORD_DETAIL, batch_uuid=batch_uuid, task_key=key
        )
        return CODEC.decode(RecordDetail, _payload(frame))

    def history(self):
        frame = self._request(Requests.HISTORY)
        return tuple(CODEC.decode(HistoryRow, row) for row in frame["batches"])

    def log_tail(self, batch_uuid, key, limit=200):
        frame = self._request(
            Requests.LOG_TAIL, batch_uuid=batch_uuid, task_key=key, limit=limit
        )
        return list(frame["lines"])

    def caches(self):
        frame = self._request(Requests.CACHES)
        return tuple(CODEC.decode(CacheCard, card) for card in frame["caches"])

    def cache_value(self, cache_name, entry_name):
        frame = self._request(
            Requests.CACHE_VALUE, cache_name=cache_name, entry_name=entry_name
        )
        return CODEC.decode(CacheValueView, _payload(frame))

    def apply_filter(self, query, builtin_only=False, scope="tasks"):
        frame = self._request(
            Requests.APPLY_FILTER,
            query=query,
            builtin_only=builtin_only,
            scope=scope,
        )
        return tuple(frame["keys"])

    def batch_options(self):
        return dict(self._request(Requests.BATCH_OPTIONS)["options"])

    def session_params(self):
        frame = self._request(Requests.SESSION_PARAMS)
        return CODEC.decode(SessionParams, _payload(frame))

    # --- subscriptions ---------------------------------------------------------

    def subscribe(self, topic, handler):
        with self._lock:
            handlers = self._handlers.setdefault(topic, [])
            if handler not in handlers:
                handlers.append(handler)
        self._ensure_subscribed()

    def unsubscribe(self, topic, handler):
        with self._lock:
            handlers = self._handlers.get(topic, [])
            if handler in handlers:
                handlers.remove(handler)

    def subscribe_task_log(self, task_key, handler):
        with self._lock:
            handlers = self._task_log_handlers.setdefault(task_key, [])
            if handler not in handlers:
                handlers.append(handler)
        # The live lines ride the session subscription (see
        # Connection.handle_subscribe_task_log), so the lane subscribes first.
        self._ensure_subscribed()
        request_id = self.wire.next_id()
        pending = self.wire.backlog_exchange(self.session_id, task_key, request_id)
        try:
            self.wire.send(
                self._task_log_frame(
                    FrameTypes.SUBSCRIBE_TASK_LOG, task_key, request_id
                )
            )
            frame = pending.wait(REQUEST_TIMEOUT)
        finally:
            self.wire.drop_backlog_exchange(self.session_id, task_key, request_id)
        if frame["type"] == FrameTypes.ERROR:
            raise ValueError(frame.get("reason", CONNECTION_DOWN))
        return tuple(frame["lines"])

    def unsubscribe_task_log(self, task_key, handler):
        with self._lock:
            handlers = self._task_log_handlers.get(task_key, [])
            if handler in handlers:
                handlers.remove(handler)
            drained = not handlers
            if drained:
                self._task_log_handlers.pop(task_key, None)
        if drained:
            self._send_quietly(
                self._task_log_frame(FrameTypes.UNSUBSCRIBE_TASK_LOG, task_key)
            )

    def close(self):
        with self._lock:
            self._handlers.clear()
            task_keys = tuple(self._task_log_handlers)
            self._task_log_handlers.clear()
        for task_key in task_keys:
            self._send_quietly(
                self._task_log_frame(FrameTypes.UNSUBSCRIBE_TASK_LOG, task_key)
            )
        if self._subscribed:
            self._subscribed = False
            self._send_quietly(
                {"type": FrameTypes.UNSUBSCRIBE, "session_id": self.session_id}
            )
        self.wire.drop_lane(self.session_id)

    def _subscribe_frame(self):
        return {"type": FrameTypes.SUBSCRIBE, "session_id": self.session_id}

    def _task_log_frame(self, frame_type, task_key, request_id=None):
        frame = {
            "type": frame_type,
            "session_id": self.session_id,
            "task_key": task_key,
        }
        if request_id is not None:
            frame["request_id"] = request_id
        return frame

    def _ensure_subscribed(self):
        with self._lock:
            if self._subscribed:
                return
            self._subscribed = True
            self._awaiting_snapshot = True
        # A send into an outage is fine: the reconnect resubscribes the lane.
        self._send_quietly(self._subscribe_frame())

    def _send_quietly(self, frame):
        try:
            self.wire.send(frame)
        except ConnectionError:
            pass

    def resubscribe(self):
        """Replay the subscriptions after a reconnect. The fresh snapshot
        heals the subscribers; the backlog replies find no pending exchange
        and are dropped, so no line duplicates into a live view."""
        if not self._subscribed and not self._task_log_handlers:
            return
        self._awaiting_snapshot = True
        self._healing = True
        self._subscribed = True
        self._send_quietly(self._subscribe_frame())
        with self._lock:
            task_keys = tuple(self._task_log_handlers)
        for task_key in task_keys:
            self._send_quietly(
                self._task_log_frame(FrameTypes.SUBSCRIBE_TASK_LOG, task_key)
            )

    # --- actions ----------------------------------------------------------------

    def submit(self, action):
        return self.wire.action(self.session_id, action)

    # --- the lane: frames in, model events out -----------------------------------

    def on_frame(self, frame):
        if frame["type"] == FrameTypes.SNAPSHOT:
            self._on_snapshot(frame)
            return
        if self._awaiting_snapshot:
            # The requested snapshot supersedes these events.
            return
        seq = frame.get("seq")
        if self._last_seq is not None and seq != self._last_seq + 1:
            self._recover()
            return
        self._last_seq = seq
        for event in self._events(frame):
            self._dispatch(event)

    def _recover(self):
        """The sequence gap recovery: a resubscribe makes the server reset
        the queue and resend the snapshot (see Connection.handle_subscribe)."""
        self._awaiting_snapshot = True
        self._healing = True
        self._send_quietly(self._subscribe_frame())

    def _on_snapshot(self, frame):
        self._last_seq = frame["seq"]
        self._awaiting_snapshot = False
        if not self._healing:
            return
        self._healing = False
        # The events of the gap are gone; the snapshot carries the state
        # they produced. Re-emit it, so a live subscriber repaints.
        snapshot = CODEC.decode(SessionSnapshot, _payload_of_snapshot(frame))
        for key, name in snapshot.tasks.items():
            self._dispatch(
                TaskStatusEvent(key=key, status=TaskStatus[name], origin=Origin.RUN)
            )
        if snapshot.status == "ENDED":
            self._dispatch(SessionEndedEvent(session_id=self.session_id))

    def _events(self, frame):
        kind = frame["type"]
        if kind == FrameTypes.TASK_STATUS:
            return (
                TaskStatusEvent(
                    key=frame["key"],
                    status=TaskStatus[frame["status"]],
                    origin=Origin(frame["origin"]),
                ),
            )
        if kind == FrameTypes.EXECUTION_STATUS:
            return (
                ExecutionStatusEvent(
                    task_key=frame["task_key"],
                    status=TaskStatus[frame["status"]],
                    batch_uuid=frame["batch_uuid"],
                    origin=Origin(frame["origin"]),
                ),
            )
        if kind == FrameTypes.BATCH_CREATED:
            return (BatchCreatedEvent(info=CODEC.decode(BatchInfo, frame["batch"])),)
        if kind == FrameTypes.BATCH_COMPLETED:
            return (BatchCompletedEvent(info=CODEC.decode(BatchInfo, frame["batch"])),)
        if kind == FrameTypes.LOG_BATCH:
            return tuple(
                LogLineEvent(
                    task_key=frame["task_key"],
                    batch_uuid=frame["batch_uuid"],
                    line=line,
                )
                for line in frame["lines"]
            )
        if kind == FrameTypes.SESSION_LOG_BATCH:
            return tuple(SessionLogEvent(line=line) for line in frame["lines"])
        if kind == FrameTypes.TASK_LOG_BATCH:
            return tuple(
                TaskLogEvent(task_key=frame["task_key"], line=line)
                for line in frame["lines"]
            )
        if kind == FrameTypes.CACHE_UPDATED:
            return (CacheUpdatedEvent(cache_name=frame["cache_name"]),)
        if kind == FrameTypes.SESSION_ENDED:
            return (SessionEndedEvent(session_id=self.session_id),)
        return ()

    def _dispatch(self, event):
        if isinstance(event, TaskLogEvent):
            with self._lock:
                handlers = tuple(self._task_log_handlers.get(event.task_key, ()))
        else:
            with self._lock:
                handlers = tuple(self._handlers.get(type(event), ()))
        for handler in handlers:
            handler(event)


def _payload_of_snapshot(frame):
    """The SessionSnapshot fields of one subscription snapshot frame: the
    envelope keys stripped, session_id kept (it is a snapshot field)."""
    return {key: value for key, value in frame.items() if key not in ("type", "seq")}
