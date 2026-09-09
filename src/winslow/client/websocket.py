"""The wire transport of the session port: the same client pair as
winslow.client.local, built from the protocol frames (see winslow.protocol).
The codec decodes every payload into the model dataclasses (see winslow.model).
This module needs the connect extra (websockets, pydantic)."""

import contextlib
import itertools
import json
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, InvalidStateError, wait
from dataclasses import asdict, dataclass, field
from enum import Enum, auto
from urllib.parse import urlsplit, urlunsplit

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect as websocket_connect

from winslow.actions import Ack, Action, BatchAck, CheckTasks, RunTasks
from winslow.client.base import AppClient, SessionClient
from winslow.protocol.codec import CODEC
from winslow.events import (
    SessionEndedEvent,
)
from winslow.exceptions import MisconfigurationError, RequestError
from winslow.logger import LOGGER
from winslow.model import (
    ConnectionEvent,
    SessionSnapshot,
    TaskLogEvent,
)
from winslow.protocol.frames import (
    AckFrame,
    ActionFrame,
    ErrorFrame,
    HelloFrame,
    HelloOkFrame,
    ResultFrame,
    SnapshotFrame,
    SubscribeFrame,
    TaskLogBacklogFrame,
    TaskLogSubscribeFrame,
    TaskLogUnsubscribeFrame,
    decode_frame,
    encode_frame,
)
from winslow.protocol.lanes import Lane, decode_lane

OPEN_TIMEOUT = 5.0
REQUEST_TIMEOUT = 60.0
RECONNECT_DELAY = 0.5
RECONNECT_DELAY_MAX = 5.0

CONNECTION_DOWN = (
    "the connection to the serve process is down - the client reconnects "
    "in the background; retry once it is back."
)


def normalize_url(url):
    """The websocket endpoint for a connect URL. ws://host:port resolves to
    the /ws route of the serve process. An explicit path stays."""
    parts = urlsplit(url)
    if parts.scheme not in ("ws", "wss"):
        raise MisconfigurationError(
            f"{url!r} is not a websocket URL - connect to ws://host:port "
            f"(or wss:// behind TLS)."
        )
    path = parts.path if parts.path not in ("", "/") else "/ws"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


@dataclass(frozen=True)
class Link:
    """One socket lifetime. A reconnect binds a new Link with empty tables,
    so no table is cleared or walked (see Wire.exchange)."""

    websocket: object
    # Settled with the ConnectionError of the drop (see fail).
    outage: Future = field(default_factory=Future)
    # Request id -> Future, settled by the receiver thread (see Wire._settle).
    pending: dict = field(default_factory=dict)
    # Request id -> lane: a refusal must reach the lane, or it waits for a snapshot forever.
    subscribes: dict = field(default_factory=dict)

    def fail(self, reason):
        """Settle the outage once. The receiver and close can both reach the
        same dead socket, and the first reason wins."""
        with contextlib.suppress(InvalidStateError):
            self.outage.set_exception(ConnectionError(reason))


class Wire:
    """One websocket to a serve process, shared by every client of the
    connection. Senders write from any thread (the sync websocket client
    locks its writes). The receiver thread reads every frame and routes it
    to a pending exchange or a session lane. On a dropped connection it
    reconnects with backoff, replays the hello, and resubscribes every lane."""

    def __init__(self, url, token=None, ticket=None, open_timeout=OPEN_TIMEOUT):
        self.url = normalize_url(url)
        self.token = token
        self.ticket = ticket
        self.open_timeout = open_timeout
        # Before connect, a Link with no socket refuses every send (see _send).
        self._link = Link(websocket=None)
        self._receiver = None
        self._closing = threading.Event()
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        # One shared lane per session id (see session_lane): the server
        # keeps one subscription per session per socket, so a second client
        # of the same session must not reset the first one's stream.
        self._lanes = {}
        # The ConnectionEvent handlers (see subscribe_connection).
        self._connection_handlers = []

    def connect(self):
        try:
            self._link = Link(self._open())
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
        link = self._link
        link.fail("the client is closed.")
        if link.websocket is not None:
            link.websocket.close()
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

    def subscribe_session(self, lane):
        """Send one subscribe frame and return its request id. A refusal
        reaches the lane through the id (see on_subscribe_refused), a snapshot
        settles it (see settle_subscribe). A send into an outage is dropped:
        the reconnect resubscribes every lane."""
        request_id = self.next_id()
        link = self._link
        link.subscribes[request_id] = lane
        try:
            self._send(
                link, SubscribeFrame(session_id=lane.session_id, request_id=request_id)
            )
        except ConnectionError:
            link.subscribes.pop(request_id, None)
        return request_id

    def settle_subscribe(self, request_id):
        """A snapshot answered the subscribe: its id is settled."""
        self._link.subscribes.pop(request_id, None)

    def subscribe_connection(self, handler):
        with self._lock:
            if handler not in self._connection_handlers:
                self._connection_handlers.append(handler)

    def _emit_connection(self, connected):
        with self._lock:
            handlers = tuple(self._connection_handlers)
        for handler in handlers:
            handler(ConnectionEvent(connected=connected))

    # --- the outgoing side -----------------------------------------------------

    def send(self, frame):
        self._send(self._link, frame)

    @classmethod
    def _send(cls, link, frame):
        try:
            link.websocket.send(encode_frame(frame))
        except (OSError, WebSocketException, AttributeError) as exc:
            raise ConnectionError(CONNECTION_DOWN) from exc

    def exchange(self, frame_class, **fields):
        """One request frame, blocking until the reply with its request id,
        or until the socket that carried it dies. A server refusal raises
        RequestError (see winslow.client.base)."""
        request_id = self.next_id()
        # A reply arrives on the socket that carried the request, so the wait is on this link.
        link = self._link
        future = Future()
        link.pending[request_id] = future
        try:
            self._send(link, frame_class(request_id=request_id, **fields))
            done, _ = wait(
                (future, link.outage),
                timeout=REQUEST_TIMEOUT,
                return_when=FIRST_COMPLETED,
            )
        finally:
            link.pending.pop(request_id, None)
        if not done:
            raise TimeoutError(
                f"no answer from the serve process within {REQUEST_TIMEOUT:g}s - "
                f"the server may be overloaded or unreachable."
            )
        if not future.done():
            raise link.outage.exception()
        frame = future.result()
        if isinstance(frame, ErrorFrame):
            raise self._refusal(frame)
        return frame

    def request(self, spec, **fields):
        """The result frame of one Read declaration (see exchange)."""
        return self.exchange(spec.envelope, **fields)

    @classmethod
    def _refusal(cls, frame):
        """The RequestError of one error frame. detail carries a server
        traceback for the create and restore error modal."""
        return RequestError(frame.reason, detail=frame.detail)

    def action(self, session_id, action):
        """One action frame, blocking until its ack. A transport failure
        answers a refused ack, the same contract as the handler
        (see ActionHandler)."""
        if not isinstance(action, Action):
            return self._refused(
                action,
                f"{type(action).__name__} names no action of the wire - "
                f"the actions are {sorted(Action.by_name)}.",
            )
        try:
            frame = self.exchange(
                ActionFrame,
                session_id=session_id,
                action=type(action).name,
                fields=asdict(action),
            )
        except (ConnectionError, TimeoutError, RequestError) as exc:
            return self._refused(action, str(exc))
        ack_class = BatchAck if "batch_uuid" in frame.ack else Ack
        return ack_class(**frame.ack)

    @classmethod
    def _refused(cls, action, reason):
        ack_class = BatchAck if isinstance(action, (RunTasks, CheckTasks)) else Ack
        return ack_class(accepted=False, reason=reason)

    # --- the receiver thread -----------------------------------------------------

    def _open(self):
        """Open one socket and complete the handshake: send hello, then read
        hello_ok."""
        websocket = websocket_connect(self.url, open_timeout=self.open_timeout)
        try:
            websocket.send(
                encode_frame(HelloFrame(token=self.token, ticket=self.ticket))
            )
            reply = decode_frame(json.loads(websocket.recv(timeout=self.open_timeout)))
            if not isinstance(reply, HelloOkFrame):
                raise MisconfigurationError(
                    f"{self.url} refused the connection - "
                    f"{getattr(reply, 'reason', 'no hello_ok answer')}"
                )
        except BaseException:
            websocket.close()
            raise
        return websocket

    def _receive_loop(self):
        while True:
            link = self._link
            try:
                raw = link.websocket.recv()
            except (OSError, WebSocketException):
                link.fail(CONNECTION_DOWN)
                if self._closing.is_set() or not self._reconnect():
                    return
                continue
            # A bad frame or a raising handler must not kill the receiver: a
            # dead receiver blocks every read until its timeout.
            try:
                self._route(decode_frame(json.loads(raw)))
            except Exception:
                LOGGER.error(f"A frame is dropped - {raw[:200]!r}", exc_info=True)

    def _reconnect(self):
        """Reopen the socket with backoff, then resubscribe every lane. A
        read during the outage raises ConnectionError. Each lane replays its
        fresh snapshot to its subscribers (see RemoteSessionClient.resubscribe).
        The ConnectionEvent lane reports the drop and the recovery."""
        self._emit_connection(False)
        delay = RECONNECT_DELAY
        while not self._closing.is_set():
            try:
                websocket = self._open()
            except MisconfigurationError as exc:
                LOGGER.warning(f"the serve process refuses the reconnect: {exc}")
            except (OSError, TimeoutError, WebSocketException, ValueError):
                pass
            else:
                # The lanes resubscribe into the new link, so it binds first.
                self._link = Link(websocket)
                with self._lock:
                    lanes = tuple(self._lanes.values())
                for lane in lanes:
                    lane.resubscribe()
                self._emit_connection(True)
                return True
            time.sleep(delay)
            delay = min(delay * 2, RECONNECT_DELAY_MAX)
        return False

    def _route(self, frame):
        match frame:
            case ErrorFrame():
                if not self._settle(frame):
                    lane = self._link.subscribes.pop(frame.request_id, None)
                    if lane is not None:
                        lane.on_subscribe_refused(frame.reason)
            case ResultFrame() | AckFrame() | TaskLogBacklogFrame():
                self._settle(frame)
            case SnapshotFrame():
                self._to_lane(frame)
            case _ if frame.type in Lane.by_type:
                self._to_lane(frame)
            case _:
                LOGGER.warning(
                    f"a {frame.type} frame has no destination - dropped: {frame!r:.200}"
                )

    def _settle(self, frame):
        """Hand a reply to the exchange that waits for its request id. False
        when no exchange waits (see exchange)."""
        future = self._link.pending.get(frame.request_id)
        if future is None:
            return False
        future.set_result(frame)
        return True

    def _to_lane(self, frame):
        lane = self._lanes.get(frame.session_id)
        if lane is not None:
            lane.on_frame(frame)


class RemoteAppClient(AppClient):
    """The dashboard scope over one serve process. connect() opens the
    socket and must run before the first read. A read the server refuses
    raises RequestError (see winslow.client.base)."""

    def __init__(self, url, token=None, ticket=None, open_timeout=OPEN_TIMEOUT):
        self.wire = Wire(url, token=token, ticket=ticket, open_timeout=open_timeout)

    def connect(self):
        self.wire.connect()
        return self

    def close(self):
        self.wire.close()

    def read(self, spec, *args, **kwargs):
        frame = self.wire.request(spec, **spec.bind(args, kwargs))
        return _decode(spec, frame)

    def session(self, session_id):
        return self.wire.session_lane(session_id)

    def subscribe_connection(self, handler):
        self.wire.subscribe_connection(handler)


def _decode(spec, frame):
    """The port value of one result frame, decoded into spec.result."""
    payload = frame.result
    if spec.result is None:
        return payload
    if spec.many:
        return tuple(CODEC.decode(spec.result, item) for item in payload)
    if spec.result in (list, tuple, dict):
        return spec.result(payload)
    return CODEC.decode(spec.result, payload)


class LaneState(Enum):
    """Where a wire lane is between the subscribe and the stream (see
    RemoteSessionClient). A subscribing lane drops frames until its snapshot
    arrives. A healing lane does the same and replays that snapshot as
    events, because its subscribers hold state from before the gap."""

    IDLE = auto()
    SUBSCRIBING = auto()
    HEALING = auto()
    STREAMING = auto()


class RemoteSessionClient(SessionClient):
    """One session over the wire. The instance is also the lane of the
    session: the wire routes every frame of the session here, and on_frame
    turns it into the model events the local adapter publishes. A handler
    runs on the receiver thread. On a sequence gap the lane resubscribes and
    replays the fresh snapshot to its subscribers (see Connection.handle_subscribe)."""

    def __init__(self, wire, session_id):
        self.wire = wire
        self.session_id = session_id
        self._lock = threading.Lock()
        # {topic: [handler, ...]} and {task key: [handler, ...]}.
        self._handlers = {}
        self._task_log_handlers = {}
        self._state = LaneState.IDLE
        # The sequence of the last dispatched frame (see on_frame).
        self._last_seq = None
        # The request id of the outstanding subscribe (see _on_snapshot).
        self._subscribe_id = None

    # --- reads ---------------------------------------------------------------

    def read(self, spec, *args, **kwargs):
        frame = self.wire.request(
            spec, session_id=self.session_id, **spec.bind(args, kwargs)
        )
        return _decode(spec, frame)

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
        # The live lines arrive over the session subscription (see
        # Connection.handle_subscribe_task_log), so the lane subscribes first.
        self._ensure_subscribed()
        frame = self.wire.exchange(
            TaskLogSubscribeFrame, session_id=self.session_id, task_key=task_key
        )
        return tuple(frame.lines)

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
                TaskLogUnsubscribeFrame(session_id=self.session_id, task_key=task_key)
            )

    def _ensure_subscribed(self):
        with self._lock:
            if self._state is not LaneState.IDLE:
                return
            self._state = LaneState.SUBSCRIBING
        self._subscribe_id = self.wire.subscribe_session(self)

    def _send_quietly(self, frame):
        try:
            self.wire.send(frame)
        except ConnectionError:
            pass

    def resubscribe(self):
        """Replay the subscriptions after a reconnect. The lane replays the
        fresh snapshot to its subscribers. The wire drops the backlog replies,
        which have no pending exchange, so a live view keeps each line once."""
        if self._state is LaneState.IDLE and not self._task_log_handlers:
            return
        self._state = LaneState.HEALING
        self._subscribe_id = self.wire.subscribe_session(self)
        with self._lock:
            task_keys = tuple(self._task_log_handlers)
        for task_key in task_keys:
            self._send_quietly(
                TaskLogSubscribeFrame(session_id=self.session_id, task_key=task_key)
            )

    # --- actions ----------------------------------------------------------------

    def submit(self, action):
        return self.wire.action(self.session_id, action)

    # --- the lane: frames in, model events out -----------------------------------

    def on_frame(self, frame):
        if isinstance(frame, SnapshotFrame):
            self._on_snapshot(frame)
            return
        if self._state in (LaneState.SUBSCRIBING, LaneState.HEALING):
            # The requested snapshot supersedes these events.
            return
        seq = frame.seq
        if self._last_seq is not None and seq != self._last_seq + 1:
            self._recover()
            return
        self._last_seq = seq
        for event in self._events(frame):
            self._dispatch(event)

    def _recover(self):
        """The sequence gap recovery: a resubscribe makes the server reset
        the queue and resend the snapshot (see Connection.handle_subscribe)."""
        self._state = LaneState.HEALING
        self._subscribe_id = self.wire.subscribe_session(self)

    def on_subscribe_refused(self, reason):
        """The server refused the subscribe of the lane: the session does not
        resolve there any more, so no snapshot answers. The end event tells
        the panes. A silent lane would wait for that snapshot forever."""
        LOGGER.warning(f"the subscribe of {self.session_id} was refused - {reason}")
        self._state = LaneState.IDLE
        self._dispatch(SessionEndedEvent(session_id=self.session_id))

    def _on_snapshot(self, frame):
        self.wire.settle_subscribe(self._subscribe_id)
        self._last_seq = frame.seq
        healed = self._state is LaneState.HEALING
        # The state moves before the replay, so a replay that raises leaves a
        # streaming lane. A healing lane would drop every frame from then on.
        self._state = LaneState.STREAMING
        if healed:
            self._replay(frame)

    def _replay(self, frame):
        """The events of the gap are gone, but the snapshot carries the state
        they produced. A live subscriber repaints from them."""
        snapshot = CODEC.decode(SessionSnapshot, frame.snapshot)
        for event in snapshot.as_events():
            self._dispatch(event)

    def _events(self, frame):
        return decode_lane(frame)

    def _dispatch(self, event):
        if isinstance(event, TaskLogEvent):
            with self._lock:
                handlers = tuple(self._task_log_handlers.get(event.task_key, ()))
        else:
            with self._lock:
                handlers = tuple(self._handlers.get(type(event), ()))
        # The same isolation as the session bus: an observer must not break
        # the operation it observes (see SessionBus).
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                LOGGER.error(
                    f"Subscriber {handler!r} failed on {type(event).__name__}.",
                    exc_info=True,
                )
