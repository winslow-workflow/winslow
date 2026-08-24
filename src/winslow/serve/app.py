"""The serve process: ServeApp owns the process state (the registry, the
credential policy, the bridges), Connection owns one socket after its hello
(the subscriptions, the control queue, the one sender task, the request
jobs). create_app builds the ASGI app from a ServeApp."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import asdict

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from winslow.actions import (
    CheckTasks,
    EndSession,
    RunTasks,
    SetBatchOptions,
    StopBatch,
)
from winslow.serve.bridge import EventBridge, Subscription
from winslow.serve.sessions import create_session
from winslow.task.info import TaskInfo

PROTOCOL_VERSION = 1

# The wire vocabulary of the actions: the frame names the action, the fields
# fill the dataclass (see winslow.actions).
ACTION_CLASSES = {
    "run_tasks": RunTasks,
    "check_tasks": CheckTasks,
    "stop_batch": StopBatch,
    "end_session": EndSession,
    "set_batch_options": SetBatchOptions,
}

# The refusal codes of the handshake. The refusal also rides a hello_error
# frame: after an accepted upgrade a browser reads the frame, the code, and
# the reason (see serve-spikes-findings, spike 1).
MALFORMED_HELLO = 4400
CREDENTIAL_REFUSED = 4401
HELLO_TIMEOUT = 4408
# The close code of a client that stays behind a full frame window.
CLIENT_TOO_SLOW = 1013


def session_row(session):
    return {
        "session_id": session.session_id,
        "workflow": str(session.workflow),
        "status": session.status.name,
    }


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
    try:
        return action_class(**fields)
    except TypeError as exc:
        raise ValueError(f"bad fields for {name}: {exc}") from None


def descriptor_rows(orchestrator):
    """One row per collected workflow, from the ConfigOption declarations:
    what a remote start form renders."""
    rows = []
    for name in orchestrator.workflow_registry.names:
        workflow_kls = orchestrator.workflow_registry[name]
        options = [
            {
                "name": option_name,
                "help": option.help_text,
                "default": option.format_value(option.default),
                "required": option.required,
                "choices": (
                    [str(choice) for choice in option.choices]
                    if option.choices
                    else None
                ),
                "multiselect": option.multiselect,
                "type": option.type.__name__ if option.type else None,
            }
            for option_name, option in workflow_kls.config_meta.items()
            if option.show_on_ui
        ]
        rows.append({"workflow": name, "options": options})
    return rows


def history_rows(session):
    """One row per batch, with the per-task outcomes of its record store."""
    runner = session.workflow.runner
    rows = []
    for batch in runner.batches:
        store = runner.record_store(batch.uuid)
        rows.append(
            {
                "uuid": batch.uuid,
                "action": batch.action.name,
                "status": batch.status.name,
                "task_count": batch.task_count,
                "created_at": batch.created_at.timestamp(),
                "completed_at": (
                    batch.completed_at.timestamp() if batch.completed_at else None
                ),
                "tasks": (
                    {key: status.name for key, status in store.items()}
                    if store is not None
                    else {}
                ),
            }
        )
    return rows


class Bridges:
    """The bridges of the serve process, one per subscribed session. Built on
    first subscribe: the snapshot carries the state, so earlier events are
    already inside it. Runs on the loop only."""

    def __init__(self, qsize):
        self.qsize = qsize
        self._bridges = {}

    def get(self, session_id):
        return self._bridges.get(session_id)

    def get_or_create(self, session):
        bridge = self._bridges.get(session.session_id)
        if bridge is None:
            bridge = EventBridge(session, qsize=self.qsize)
            bridge.attach()
            bridge.start()
            self._bridges[session.session_id] = bridge
        return bridge

    def close(self):
        for bridge in self._bridges.values():
            bridge.close()
        self._bridges.clear()


class ServeApp:
    """One serve process: the live sessions of the registry over one
    websocket endpoint. orchestrator and state_store power the descriptor
    and create_session requests; without them those requests answer with an
    error."""

    def __init__(
        self,
        registry,
        credentials,
        hello_timeout=5.0,
        qsize=10_000,
        orchestrator=None,
        state_store=None,
    ):
        self.registry = registry
        self.credentials = credentials
        self.hello_timeout = hello_timeout
        self.qsize = qsize
        self.orchestrator = orchestrator
        self.state_store = state_store
        self.bridges = Bridges(qsize)

    def starlette(self):
        return Starlette(
            routes=[WebSocketRoute("/ws", self.ws)], lifespan=self._lifespan
        )

    @asynccontextmanager
    async def _lifespan(self, app):
        # The composition point: the MCP session manager mounts here later.
        try:
            yield
        finally:
            self.bridges.close()

    async def ws(self, websocket):
        await websocket.accept()
        user = await self._handshake(websocket)
        if user is None:
            return
        await Connection(self, websocket, user).run()

    async def _refuse(self, websocket, code, reason):
        await websocket.send_json({"type": "hello_error", "reason": reason})
        await websocket.close(code=code, reason=reason)

    async def _handshake(self, websocket):
        """Returns the user, or None after a refusal."""
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), self.hello_timeout)
        except asyncio.TimeoutError:
            await self._refuse(
                websocket, HELLO_TIMEOUT, f"no hello within {self.hello_timeout:g}s"
            )
            return None
        try:
            hello = json.loads(raw)
            if hello.get("type") != "hello":
                raise ValueError
        except (ValueError, AttributeError):
            await self._refuse(
                websocket, MALFORMED_HELLO, "the first message must be a hello"
            )
            return None
        user, error = self.credentials.verify_hello(
            hello, websocket.headers.get("origin")
        )
        if error:
            await self._refuse(websocket, CREDENTIAL_REFUSED, error)
            return None
        return user


class Connection:
    """One socket after its hello. The receive loop dispatches the frames;
    one sender task sends everything (the control queue and every
    subscription), so no two tasks write the socket."""

    def __init__(self, app, websocket, user):
        self.app = app
        self.websocket = websocket
        self.user = user
        self.wake = asyncio.Event()
        self.control = Subscription(wake=self.wake, maxlen=app.qsize)
        self.subscriptions = {}
        self.jobs = set()

    async def run(self):
        await self.websocket.send_json(
            {"type": "hello_ok", "user": self.user, "version": PROTOCOL_VERSION}
        )
        await self.websocket.send_json(
            {
                "type": "snapshot",
                "seq": 0,
                "sessions": [session_row(s) for s in self.app.registry.sessions()],
            }
        )
        send_task = asyncio.get_running_loop().create_task(self._sender())
        try:
            while True:
                try:
                    frame = await self.websocket.receive_json()
                except (WebSocketDisconnect, RuntimeError):
                    return
                except ValueError:
                    self.reply({"type": "error", "reason": "the message is not JSON"})
                    continue
                self.handle_frame(frame)
        finally:
            send_task.cancel()
            for job in self.jobs:
                job.cancel()
            for session_id, subscription in self.subscriptions.items():
                bridge = self.app.bridges.get(session_id)
                if bridge is not None:
                    bridge.unsubscribe(subscription)

    # --- the outgoing side ---------------------------------------------------

    def reply(self, payload):
        self.control.push(json.dumps(payload))

    def request_error(self, frame, reason):
        self.reply(
            {"type": "error", "request_id": frame.get("request_id"), "reason": reason}
        )

    def result(self, frame, **payload):
        self.reply(
            {
                "type": "result",
                "request_id": frame.get("request_id"),
                "kind": frame.get("kind"),
                **payload,
            }
        )

    async def _sender(self):
        while True:
            self.wake.clear()
            for subscription in (self.control, *self.subscriptions.values()):
                while subscription.deque:
                    await self.websocket.send_text(subscription.deque.popleft())
                if subscription.behind_a_full_window:
                    await self.websocket.close(
                        code=CLIENT_TOO_SLOW,
                        reason="the client stays behind a full frame window - "
                        "reconnect and subscribe for a fresh snapshot",
                    )
                    return
            await self.wake.wait()

    # --- the incoming side -----------------------------------------------------

    def handle_frame(self, frame):
        kind = frame.get("type")
        if kind == "subscribe":
            self.handle_subscribe(frame)
        elif kind == "unsubscribe":
            self.handle_unsubscribe(frame.get("session_id"))
        elif kind == "action":
            self.spawn(self.run_action(frame))
        elif kind == "request":
            self.spawn(self.run_request(frame))
        else:
            self.reply(
                {
                    "type": "error",
                    "reason": f"unknown message type {kind!r} - this server "
                    f"speaks subscribe, unsubscribe, action, and request.",
                }
            )

    def spawn(self, coroutine):
        job = asyncio.get_running_loop().create_task(coroutine)
        self.jobs.add(job)
        job.add_done_callback(self.jobs.discard)

    def resolve_for(self, frame):
        """The live session of the frame, or None after an error reply."""
        session = self.app.registry.get(frame.get("session_id"))
        if session is None:
            self.request_error(
                frame,
                f"session id {frame.get('session_id')!r} does not resolve to a "
                f"live session - it ended, or it belongs to another process.",
            )
        return session

    def handle_subscribe(self, frame):
        """Attach, snapshot, and queue - synchronous on the loop, so no drain
        pass lands between the attach and the snapshot. A second subscribe of
        one session resets the queue and resends the snapshot: that is the
        recovery of a client that saw a sequence gap."""
        session = self.resolve_for(frame)
        if session is None:
            return
        session_id = session.session_id
        bridge = self.app.bridges.get_or_create(session)
        subscription = self.subscriptions.get(session_id)
        if subscription is None:
            subscription = Subscription(wake=self.wake, maxlen=self.app.qsize)
            self.subscriptions[session_id] = subscription
        else:
            subscription.deque.clear()
            subscription.dropped = 0
        bridge.subscribe(subscription)
        subscription.push(json.dumps(bridge.snapshot()))

    def handle_unsubscribe(self, session_id):
        subscription = self.subscriptions.pop(session_id, None)
        bridge = self.app.bridges.get(session_id)
        if subscription is not None and bridge is not None:
            bridge.unsubscribe(subscription)
        self.reply({"type": "unsubscribed", "session_id": session_id})

    async def run_action(self, frame):
        session = self.resolve_for(frame)
        if session is None:
            return
        try:
            action = build_action(frame.get("action"), frame.get("fields"))
        except ValueError as exc:
            self.request_error(frame, str(exc))
            return
        # The admission gate can block: the submit runs on a worker thread.
        ack = await asyncio.to_thread(session.actions.submit_guarded, action)
        self.reply(
            {"type": "ack", "request_id": frame.get("request_id"), **asdict(ack)}
        )

    async def run_request(self, frame):
        kind = frame.get("kind")
        handlers = {
            "create_session": self._request_create_session,
            "descriptors": self._request_descriptors,
            "history": self._request_history,
            "log_tail": self._request_log_tail,
            "task_detail": self._request_task_detail,
        }
        handler = handlers.get(kind)
        if handler is None:
            self.request_error(
                frame,
                f"{kind!r} names no request. The requests are "
                f"{', '.join(sorted(handlers))}.",
            )
            return
        await handler(frame)

    async def _request_descriptors(self, frame):
        if self.app.orchestrator is None:
            self.request_error(frame, "this server serves no workflows")
            return
        self.result(frame, workflows=descriptor_rows(self.app.orchestrator))

    async def _request_create_session(self, frame):
        if self.app.orchestrator is None or self.app.state_store is None:
            self.request_error(frame, "this server creates no sessions")
            return
        try:
            session = await asyncio.to_thread(
                create_session,
                self.app.orchestrator,
                self.app.state_store,
                self.app.registry,
                frame.get("workflow"),
                frame.get("overrides"),
                frame.get("values"),
            )
        except Exception as exc:
            self.request_error(frame, str(exc.args[0] if exc.args else exc))
            return
        self.result(frame, **session_row(session))

    async def _request_history(self, frame):
        session = self.resolve_for(frame)
        if session is not None:
            self.result(frame, batches=history_rows(session))

    async def _request_log_tail(self, frame):
        session = self.resolve_for(frame)
        if session is None:
            return
        store = session.workflow.runner.record_store(frame.get("batch_uuid"))
        if store is None:
            self.request_error(
                frame,
                f"batch {frame.get('batch_uuid')!r} keeps no records in this "
                f"session.",
            )
            return
        try:
            record = store.get_record(frame.get("task_key"))
        except KeyError:
            self.request_error(
                frame,
                f"task {frame.get('task_key')!r} is not in the roster of "
                f"batch {frame.get('batch_uuid')!r}.",
            )
            return
        limit = frame.get("limit") or 200
        self.result(
            frame,
            task_key=frame.get("task_key"),
            batch_uuid=frame.get("batch_uuid"),
            lines=list(record.logs)[-limit:],
        )

    async def _request_task_detail(self, frame):
        session = self.resolve_for(frame)
        if session is None:
            return
        try:
            task = session.workflow.task_index.resolve(frame.get("task_key"))
        except KeyError as exc:
            self.request_error(frame, exc.args[0])
            return
        root_dir = (
            getattr(self.app.orchestrator.orchestrator_config, "directory", None)
            if self.app.orchestrator is not None
            else None
        )
        # The full capture evaluates user code: a worker thread runs it.
        info = await asyncio.to_thread(
            TaskInfo.from_task, task, full=True, root_dir=root_dir
        )
        self.result(frame, info=asdict(info))


def create_app(registry, credentials, hello_timeout=5.0, qsize=10_000, **kwargs):
    """The ASGI app of one serve process (see ServeApp)."""
    return ServeApp(
        registry, credentials, hello_timeout=hello_timeout, qsize=qsize, **kwargs
    ).starlette()
