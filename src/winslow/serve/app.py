"""The serve process: ServeApp owns the process state (the registry, the
credential policy, the bridges), Connection owns one socket after its hello
(the session queues, the control queue, the one sender task, the request
jobs). create_app builds the ASGI app from a ServeApp."""

import asyncio
import json
import traceback
from contextlib import asynccontextmanager
from starlette.applications import Starlette
from starlette.routing import Mount, WebSocketRoute

from winslow.actions import Action
from winslow.client import LocalAppClient
from winslow.protocol.codec import CODEC, ValidationError, report
from winslow.exceptions import MisconfigurationError, RequestError
from winslow.logger import LOGGER
from winslow.serve.bridge import EventBridge, FrameQueue
from winslow._meta import _tagged, handles
from winslow.client.base import PORT_READS
from winslow.protocol.frames import (
    AckFrame,
    ActionFrame,
    ErrorFrame,
    HelloErrorFrame,
    HelloFrame,
    HelloOkFrame,
    RequestFrame,
    ResultFrame,
    SubscribeFrame,
    TaskLogBacklogFrame,
    TaskLogSubscribeFrame,
    TaskLogUnsubscribeFrame,
    UnsubscribeFrame,
    encode_frame,
)

# The refusals of a port read: an unknown session id, or a served refusal
# (see ServeApp.read).
READ_REFUSALS = (KeyError, RequestError)

# The refusal codes of the handshake. The reason also travels as a hello_error
# frame before the close, so a browser client can read it.
MALFORMED_HELLO = 4400
CREDENTIAL_REFUSED = 4401
HELLO_TIMEOUT = 4408
# The close code of a client that stays behind a full frame window.
CLIENT_TOO_SLOW = 1013


def parse_frame(message):
    """The frame dict of one inbound ASGI receive message. A message that is
    not a JSON object raises ValueError with the reason for the client. The
    text key check refuses a binary message, which Starlette's receive_json
    turns into a KeyError."""
    text = message.get("text")
    if text is None:
        raise ValueError("a frame must be a text message.")
    try:
        frame = json.loads(text)
    except ValueError:
        raise ValueError("the message is not JSON.") from None
    if not isinstance(frame, dict):
        raise ValueError(f"a frame must be a JSON object, not {type(frame).__name__}.")
    return frame


class Bridges:
    """The bridges of the serve process, one per subscribed session. A bridge
    is built on the first subscribe. The snapshot carries the state, so earlier
    events are already inside it. Runs on the loop only."""

    def __init__(self, qsize):
        self.qsize = qsize
        self._bridges = {}

    def get(self, session_id):
        return self._bridges.get(session_id)

    def get_or_create(self, session):
        bridge = self._bridges.get(session.session_id)
        if bridge is None:
            bridge = EventBridge(session, qsize=self.qsize, owner=self)
            bridge.attach()
            bridge.start()
            self._bridges[session.session_id] = bridge
        return bridge

    def discard(self, session_id):
        """Drop the entry of a retired bridge (see EventBridge._retire)."""
        self._bridges.pop(session_id, None)

    def close(self):
        for bridge in self._bridges.values():
            bridge.close()
        self._bridges.clear()


class ServeApp:
    """One serve process: the live sessions of the registry behind two
    optional doors, the websocket endpoint and the MCP mount. Each door works
    alone, and both share the registry and the credential policy. The port
    carries the orchestrator and the state store (see LocalAppClient)."""

    def __init__(
        self,
        registry,
        credentials,
        *,
        orchestrator,
        state_store,
        hello_timeout=5.0,
        qsize=10_000,
        ws=True,
        mcp=False,
        base_url="http://127.0.0.1:8866",
    ):
        self.registry = registry
        self.credentials = credentials
        self.hello_timeout = hello_timeout
        self.qsize = qsize
        # The in-process port every door serves (see PORT_READS in winslow.client.base).
        self.local = LocalAppClient(
            registry, orchestrator=orchestrator, state_store=state_store
        )
        self.bridges = Bridges(qsize)
        self.ws_enabled = ws
        self.base_url = base_url
        self.mcp_endpoint = self._build_mcp() if mcp else None
        if not ws and self.mcp_endpoint is None:
            raise MisconfigurationError(
                "The serve process needs at least one endpoint - "
                "enable the websocket, the MCP mount, or both."
            )

    async def read(self, spec, session_id, fields):
        """One port read on a worker thread, for both doors. A refusal raises
        RequestError with the served reason. A traceback read runs project
        init code, so a broad catch answers, with the traceback for the error
        modal of a client (see RequestError.detail)."""
        caught = Exception if spec.traceback else READ_REFUSALS
        try:
            return await asyncio.to_thread(self._port_call, spec, session_id, fields)
        except caught as exc:
            reason = str(exc.args[0] if exc.args else exc)
            detail = traceback.format_exc() if spec.traceback else None
            raise RequestError(reason, detail=detail) from exc

    async def submit(self, session_id, action):
        """One guarded submit on a worker thread, for both doors. An unknown
        session id raises RequestError (see SessionRegistry.resolve)."""
        try:
            session = self.registry.resolve(session_id)
        except KeyError as exc:
            raise RequestError(exc.args[0]) from exc
        # The admission gate can block, so the submit runs on a worker thread.
        return await asyncio.to_thread(session.actions.submit_guarded, action)

    def _port_call(self, spec, session_id, fields):
        """Runs on the worker thread, so a session resolve refusal stays off
        the event loop."""
        client = self.local
        if spec.scope == "session":
            client = client.session(session_id)
        return getattr(client, spec.name)(**fields)

    def _build_mcp(self):
        try:
            from winslow.serve.mcp import McpEndpoint
        except ImportError as e:
            raise MisconfigurationError(
                "The MCP endpoint requires the mcp extra - install with: "
                "pip install 'winslow[mcp]'"
            ) from e
        return McpEndpoint(self, self.base_url)

    def starlette(self):
        routes = []
        if self.ws_enabled:
            routes.append(WebSocketRoute("/ws", self.ws))
        if self.mcp_endpoint is not None:
            routes.append(Mount("/", app=self.mcp_endpoint.streamable_http_app()))
        return Starlette(routes=routes, lifespan=self._lifespan)

    @asynccontextmanager
    async def _lifespan(self, app):
        try:
            if self.mcp_endpoint is not None:
                # A mounted MCP app starts under the parent lifespan.
                async with self.mcp_endpoint.session_manager.run():
                    yield
            else:
                yield
        finally:
            self.bridges.close()

    async def ws(self, websocket):
        await websocket.accept()
        user = await self._handshake(websocket)
        if user is None:
            return
        await Connection(self, websocket, user).run()

    async def _refuse(self, websocket, code, reason, detail=None):
        await websocket.send_text(
            encode_frame(HelloErrorFrame(reason=reason, detail=detail))
        )
        await websocket.close(code=code, reason=reason)

    async def _handshake(self, websocket):
        """Returns the user, or None after a refusal or a disconnect. The hello
        decodes through HelloFrame like every later frame (see Connection.decode),
        so verify_hello reads typed fields."""
        try:
            message = await asyncio.wait_for(websocket.receive(), self.hello_timeout)
        except asyncio.TimeoutError:
            await self._refuse(
                websocket, HELLO_TIMEOUT, f"no hello within {self.hello_timeout:g}s"
            )
            return None
        if message["type"] == "websocket.disconnect":
            return None
        try:
            frame = parse_frame(message)
            if frame.get("type") != HelloFrame.type:
                raise ValueError("the first message must be a hello.")
            hello = CODEC.decode(HelloFrame, frame)
        except ValidationError as exc:
            await self._refuse(
                websocket, MALFORMED_HELLO, "the hello is malformed", detail=report(exc)
            )
            return None
        except ValueError as exc:
            await self._refuse(websocket, MALFORMED_HELLO, str(exc))
            return None
        user, error = self.credentials.verify_hello(
            hello, websocket.headers.get("origin")
        )
        if error:
            await self._refuse(websocket, CREDENTIAL_REFUSED, error)
            return None
        return user


class Connection:
    """One socket after its hello. The receive loop dispatches the frames. One
    sender task sends everything (the control queue and every session queue),
    so no two tasks write the socket."""

    def __init__(self, app, websocket, user):
        self.app = app
        self.websocket = websocket
        self.user = user
        self.wake = asyncio.Event()
        self.control = FrameQueue(wake=self.wake, maxlen=app.qsize)
        # session_id -> FrameQueue. The task log keys of a session live on its
        # queue, so removing the queue releases both (see EventBridge).
        self.queues = {}
        self.jobs = set()

    async def run(self):
        await self.websocket.send_text(encode_frame(HelloOkFrame(user=self.user)))
        send_task = asyncio.get_running_loop().create_task(self._sender())
        try:
            while True:
                message = await self.websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                try:
                    frame = parse_frame(message)
                except ValueError as exc:
                    self.reply(ErrorFrame(reason=str(exc)))
                    continue
                self.handle_frame(frame)
        finally:
            send_task.cancel()
            for job in self.jobs:
                job.cancel()
            for session_id, queue in self.queues.items():
                bridge = self.app.bridges.get(session_id)
                if bridge is not None:
                    bridge.remove_queue(queue)

    # --- the outgoing side ---------------------------------------------------

    def reply(self, frame):
        self.control.push(encode_frame(frame))

    def request_error(self, request_id, reason, detail=None):
        self.reply(ErrorFrame(request_id=request_id, reason=reason, detail=detail))

    async def _sender(self):
        while True:
            self.wake.clear()
            for queue in (self.control, *self.queues.values()):
                while queue.deque:
                    await self.websocket.send_text(queue.deque.popleft())
                if queue.behind_a_full_window:
                    await self.websocket.close(
                        code=CLIENT_TOO_SLOW,
                        reason="the client stays behind a full frame window - "
                        "reconnect and subscribe for a fresh snapshot",
                    )
                    return
            await self.wake.wait()

    # --- the incoming side -----------------------------------------------------

    def handle_frame(self, frame):
        entry = self.inbound.get(frame.get("type"))
        if entry is None:
            self.reply(
                ErrorFrame(
                    reason=f"unknown message type {frame.get('type')!r} - this "
                    f"server speaks {', '.join(self.inbound)}."
                )
            )
            return
        frame_class, handler = entry
        envelope = self.decode(frame, frame_class)
        if envelope is not None:
            handler(self, envelope)

    def decode(self, frame, frame_class):
        """The envelope of one inbound frame, or None after an error reply.
        Every inbound frame decodes through its envelope before a handler
        sees it: the envelope replaces a trusted frame.get(...) read (see
        envelope_class, winslow.protocol.codec)."""
        try:
            return CODEC.decode(self.envelope_class(frame, frame_class), frame)
        except ValidationError as exc:
            self.request_error(
                frame.get("request_id"),
                f"the {frame.get('type')} frame is malformed - {report(exc)}",
            )
        except ValueError as exc:
            self.request_error(frame.get("request_id"), str(exc))
        return None

    @classmethod
    def envelope_class(cls, frame, frame_class):
        """The class that validates one inbound frame. A request validates
        through the envelope of the read its kind names (see Read.envelope)."""
        if frame_class is not RequestFrame:
            return frame_class
        spec = PORT_READS.get(frame.get("kind"))
        if spec is None:
            raise ValueError(
                f"{frame.get('kind')!r} names no request. The requests are "
                f"{', '.join(sorted(PORT_READS))}."
            )
        return spec.envelope

    def spawn(self, envelope, coroutine):
        job = asyncio.get_running_loop().create_task(
            self._answered(envelope, coroutine)
        )
        self.jobs.add(job)
        job.add_done_callback(self.jobs.discard)

    async def _answered(self, envelope, coroutine):
        """A failed job answers an error frame, so the client stops waiting."""
        try:
            await coroutine
        except Exception:
            LOGGER.error(
                f"{envelope.type} frame failed inside the server", exc_info=True
            )
            self.request_error(
                envelope.request_id,
                f"the {envelope.type} failed inside the server - the "
                f"server log has the traceback.",
            )

    def resolve(self, session_id, request_id):
        """The live session under session_id, or None after an error reply."""
        try:
            return self.app.registry.resolve(session_id)
        except KeyError as exc:
            LOGGER.debug(f"request {request_id!r}: {exc.args[0]}")
            self.request_error(request_id, exc.args[0])
            return None

    def resolve_for(self, envelope):
        return self.resolve(envelope.session_id, envelope.request_id)

    @handles(SubscribeFrame)
    def handle_subscribe(self, envelope):
        """Attach, snapshot, and queue, synchronous on the loop, so no drain
        pass runs between the attach and the snapshot. A second subscribe of
        one session resets the queue and resends the snapshot. This is how a
        client recovers from a sequence gap."""
        session = self.resolve_for(envelope)
        if session is None:
            return
        if session.has_ended:
            # The bus of an ended session is closed, so a new bridge has nothing
            # to attach to. The refusal reaches the lane (see on_subscribe_refused).
            self.request_error(
                envelope.request_id,
                f"{session.session_id} has ended and emits no more events - "
                f"request its history instead of subscribing.",
            )
            return
        session_id = session.session_id
        bridge = self.app.bridges.get_or_create(session)
        queue = self.queues.get(session_id)
        if queue is None:
            queue = FrameQueue(wake=self.wake, maxlen=self.app.qsize)
            self.queues[session_id] = queue
        else:
            queue.deque.clear()
            queue.dropped = 0
        bridge.add_queue(queue)
        queue.push(encode_frame(bridge.snapshot()))

    @handles(UnsubscribeFrame)
    def handle_unsubscribe(self, envelope):
        session_id = envelope.session_id
        queue = self.queues.pop(session_id, None)
        bridge = self.app.bridges.get(session_id)
        if queue is not None and bridge is not None:
            bridge.remove_queue(queue)

    @handles(TaskLogSubscribeFrame)
    def handle_subscribe_task_log(self, envelope):
        """The backlog and the live stream of one task log, outside any batch.
        The backlog answers at once. The live lines travel on the session
        subscription as task_log_batch frames, so the client subscribes to the
        session first."""
        session = self.resolve_for(envelope)
        if session is None:
            return
        if session.session_id not in self.queues:
            self.request_error(
                envelope.request_id,
                f"subscribe to {session.session_id!r} before subscribing "
                f"to one of its task logs - task_log_batch frames ride the "
                f"session subscription.",
            )
            return
        if session.has_ended:
            self.request_error(
                envelope.request_id,
                f"{session.session_id} has ended - its live task state is released.",
            )
            return
        bridge = self.app.bridges.get_or_create(session)
        try:
            backlog = bridge.subscribe_task_log(
                self.queues[session.session_id], envelope.task_key
            )
        except RequestError as exc:
            self.request_error(envelope.request_id, str(exc))
            return
        self.reply(
            TaskLogBacklogFrame(
                request_id=envelope.request_id,
                session_id=session.session_id,
                task_key=envelope.task_key,
                lines=tuple(backlog),
            )
        )

    @handles(TaskLogUnsubscribeFrame)
    def handle_unsubscribe_task_log(self, envelope):
        session_id = envelope.session_id
        task_key = envelope.task_key
        queue = self.queues.get(session_id)
        bridge = self.app.bridges.get(session_id)
        if queue is not None and bridge is not None:
            bridge.unsubscribe_task_log(queue, task_key)

    @handles(ActionFrame)
    def handle_action(self, envelope):
        self.spawn(envelope, self.run_action(envelope))

    @handles(RequestFrame)
    def handle_request(self, envelope):
        self.spawn(envelope, self.run_read(envelope, PORT_READS[envelope.kind]))

    async def run_action(self, envelope):
        """One action as an ack frame, or an error frame (see ServeApp.submit)."""
        try:
            action = Action.build(envelope.action, envelope.fields)
            ack = await self.app.submit(envelope.session_id, action)
        except (ValueError, RequestError) as exc:
            self.request_error(envelope.request_id, str(exc))
            return
        self.reply(AckFrame(request_id=envelope.request_id, ack=ack))

    async def run_read(self, envelope, spec):
        """One port read as a result frame, or an error frame (see ServeApp.read)."""
        fields = {name: getattr(envelope, name) for name in spec.fields}
        try:
            result = await self.app.read(
                spec, getattr(envelope, "session_id", None), fields
            )
        except RequestError as exc:
            self.request_error(envelope.request_id, str(exc), detail=exc.detail)
            return
        self.reply(
            ResultFrame(
                request_id=envelope.request_id, kind=envelope.kind, result=result
            )
        )


# The inbound table of handle_frame: frame type -> (frame class, handler),
# from every method @handles marks.
Connection.inbound = {
    method.handles.type: (method.handles, method)
    for method in _tagged(Connection, "handles")
}


def create_app(registry, credentials, *, orchestrator, state_store, **kwargs):
    """The ASGI app of one serve process (see ServeApp)."""
    return ServeApp(
        registry,
        credentials,
        orchestrator=orchestrator,
        state_store=state_store,
        **kwargs,
    ).starlette()
