"""The serve process: ServeApp owns the process state (the registry, the
authenticator, the bridges), Connection owns one socket after its hello
(the session queues, the control queue, the one sender task, the request
jobs). create_app builds the ASGI app from a ServeApp."""

import asyncio
import json
import socket
import time
from contextlib import asynccontextmanager
from importlib.resources import files as package_files
from pathlib import Path
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocketState

from winslow.conf import settings
from winslow.constants import ANY_HOSTS, DEFAULT_ENDPOINTS, ENDPOINTS, LOOPBACK_HOSTS
from winslow.client import LocalAppClient
from winslow.protocol.codec import CODEC, ValidationError, report
from winslow.protocol.closes import (
    CLIENT_TOO_SLOW,
    CREDENTIAL_EXPIRED,
    CREDENTIAL_REFUSED,
    EXPIRED_REASON,
    HELLO_TIMEOUT,
    MALFORMED_HELLO,
    ORIGIN_REFUSED,
    TOO_SLOW_REASON,
)
from winslow.exceptions import CredentialRefused, MisconfigurationError, RequestError
from winslow.logger import LOGGER
from winslow.serve.bridge import EventBridge, FrameQueue
from winslow.access.authz import check
from winslow.access.permissions import Permission, wire_scopes
from winslow.task.context import scoped_permissions
from winslow._meta import _tagged, handles
from winslow.client.base import PORT_ACTIONS, PORT_READS
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


@asynccontextmanager
async def until_expiry(permissions):
    """Run the block until the credential expires. The yielded bound answers
    expired() afterwards, so the caller closes the socket once the sender task
    is gone: one task writes the socket (see Connection). A credential without
    an expiry bounds nothing. A stale permission thus lives at most one
    credential lifetime (see PermissionContext.remaining)."""
    try:
        async with asyncio.timeout(permissions.remaining()) as bound:
            yield bound
    except TimeoutError:
        if not bound.expired():
            raise


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


def page_origins(host, port):
    """The origins a browser sends for the web UI the serve process hands
    out itself. A loopback bind answers under every loopback name, a bind on
    every interface under the loopback names and the name of the host."""
    if host in LOOPBACK_HOSTS:
        hosts = LOOPBACK_HOSTS
    elif host in ANY_HOSTS:
        hosts = LOOPBACK_HOSTS + (socket.gethostname(),)
    else:
        hosts = (host,)
    return tuple(
        f"http://[{name}]:{port}" if ":" in name else f"http://{name}:{port}"
        for name in hosts
    )


def public_origin():
    """The origin of SERVE_PUBLIC_URL, the address clients reach the server on, or
    None without one."""
    if settings.SERVE_PUBLIC_URL is None:
        return None
    parts = urlsplit(settings.SERVE_PUBLIC_URL)
    return f"{parts.scheme}://{parts.netloc}"


def serve_origins(host, port, endpoints):
    """The browser origins the websocket accepts: SERVE_ORIGINS, the origin
    of SERVE_PUBLIC_URL, plus the origins of the served page when the web endpoint
    is on."""
    listed = tuple(settings.SERVE_ORIGINS)
    public = (public_origin(),) if public_origin() else ()
    served = page_origins(host, port) if "web" in endpoints else ()
    return listed + public + served


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
    """One serve process: the live sessions of the registry behind the
    endpoints of ENDPOINTS: the websocket, the web UI at the root, and the
    MCP mount. The endpoints share the registry and the authenticator.
    The port carries the orchestrator and the state store (see LocalAppClient)."""

    def __init__(
        self,
        registry,
        authenticator,
        *,
        orchestrator,
        state_store,
        hello_timeout=5.0,
        qsize=10_000,
        endpoints=DEFAULT_ENDPOINTS,
        base_url="http://127.0.0.1:8866",
        static_dir=None,
        allowed_origins=None,
    ):
        self.registry = registry
        self.authenticator = authenticator
        self.allowed_origins = allowed_origins
        self.hello_timeout = hello_timeout
        self.qsize = qsize
        # The in-process port every endpoint serves (see PORT_READS in winslow.client.base).
        self.local = LocalAppClient(
            registry, orchestrator=orchestrator, state_store=state_store
        )
        self.bridges = Bridges(qsize)
        self.endpoints = frozenset(endpoints)
        if unknown := self.endpoints - set(ENDPOINTS):
            raise MisconfigurationError(
                f"{sorted(unknown)} name no serve endpoint. The endpoints are {list(ENDPOINTS)}."
            )
        if "web" in self.endpoints and "ws" not in self.endpoints:
            raise MisconfigurationError(
                "The web endpoint needs the ws endpoint: the page talks to the "
                "websocket - serve both, or leave web out."
            )
        self.base_url = base_url
        self.static_dir = (
            self._resolve_static(static_dir) if "web" in self.endpoints else None
        )
        self.mcp_endpoint = self._build_mcp() if "mcp" in self.endpoints else None

    def close_streams(self):
        """End the streams a client holds open across requests. The shutdown
        calls this before the connection wait, so the wait ends when the
        clients read the final frame (see HttpServer)."""
        if self.mcp_endpoint is not None:
            self.mcp_endpoint.close_listen_streams()

    def drain(self, timeout):
        """Stop every running batch and wait for the workers, up to timeout
        seconds in total. Returns the batches still running after the bound.
        The serve handler calls this after uvicorn returns: the batch workers
        are plain threads, and the interpreter waits for them at exit."""
        batches = [
            batch
            for session in self.registry.sessions()
            for batch in session.active_batches
        ]
        for batch in batches:
            batch.request_stop()
        deadline = time.monotonic() + timeout
        stuck = [
            batch
            for batch in batches
            if not batch.join(max(0.0, deadline - time.monotonic()))
        ]
        for batch in stuck:
            LOGGER.warning(
                f"batch {batch.uuid} is still running {timeout}s after the stop "
                f"request - the process waits for its current task."
            )
        return stuck

    async def read(self, spec, session_id, fields):
        """One port read on a worker thread, for both endpoints. A refusal raises
        RequestError with the served reason."""
        try:
            return await asyncio.to_thread(self._port_call, spec, session_id, fields)
        except READ_REFUSALS as exc:
            reason = str(exc.args[0] if exc.args else exc)
            raise RequestError(reason) from exc

    async def submit(self, action, session_id=None):
        """One guarded submit on a worker thread, for both endpoints: an app
        action to the handler of the port, a session action to the handler of
        the named session. An unknown session id raises RequestError (see
        SessionRegistry.resolve)."""
        if action.spec.scope == "app":
            return await asyncio.to_thread(self.local.actions.submit_guarded, action)
        if session_id is None:
            raise RequestError(
                f"{action.spec.name} is a session action - the frame names the "
                f"session in session_id."
            )
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

    @classmethod
    def _resolve_static(cls, static_dir):
        """The directory of the built web UI (see web/README.md), or None for
        a checkout without a build."""
        path = Path(
            str(package_files("winslow.serve") / "static")
            if static_dir is None
            else static_dir
        )
        if not (path / "index.html").is_file():
            LOGGER.info(f"no web UI build at {path} - serving the endpoints alone.")
            return None
        return path

    def starlette(self):
        routes = []
        if "ws" in self.endpoints:
            routes.append(WebSocketRoute("/ws", self.ws))
        if self.static_dir is not None:
            # The UI owns / and /assets alone. The MCP mount keeps every other
            # path, including the /mcp redirect and the OAuth metadata.
            routes.append(Route("/", self.index))
            assets = self.static_dir / "assets"
            if assets.is_dir():
                # A front application on a listed origin embeds the page and
                # loads the module script from here: a module needs CORS.
                files = CORSMiddleware(
                    StaticFiles(directory=assets),
                    allow_origins=list(self.allowed_origins or ()),
                )
                routes.append(Mount("/assets", app=files))
        if self.mcp_endpoint is not None:
            routes.append(Mount("/", app=self.mcp_endpoint.streamable_http_app()))
        return Starlette(routes=routes, lifespan=self._lifespan)

    async def index(self, request):
        return FileResponse(self.static_dir / "index.html")

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
        permissions = await self._handshake(websocket)
        if permissions is None:
            return
        await Connection(self, websocket, permissions).run()

    def origin_allowed(self, origin):
        """The Origin rule of a browser page. It holds on every bind: a page
        in a local browser reaches a loopback server too. A client without a
        browser sends no Origin and passes."""
        return origin is None or origin in (self.allowed_origins or ())

    async def _refuse(self, websocket, code, reason, detail=None):
        await websocket.send_text(
            encode_frame(HelloErrorFrame(reason=reason, detail=detail))
        )
        await websocket.close(code=code, reason=reason)

    async def _handshake(self, websocket):
        """Returns the PermissionContext, or None after a refusal or a
        disconnect. The hello decodes through HelloFrame like every later frame
        (see Connection.decode), so verify_hello reads typed fields."""
        try:
            message = await asyncio.wait_for(websocket.receive(), self.hello_timeout)
        except asyncio.TimeoutError:
            await self._refuse(
                websocket,
                HELLO_TIMEOUT,
                f"no connection request within {self.hello_timeout:g}s",
            )
            return None
        if message["type"] == "websocket.disconnect":
            return None
        try:
            frame = parse_frame(message)
            if frame.get("type") != HelloFrame.type:
                raise ValueError("the first message must be the connection request.")
            hello = CODEC.decode(HelloFrame, frame)
        except ValidationError as exc:
            await self._refuse(
                websocket,
                MALFORMED_HELLO,
                "the connection request is malformed",
                detail=report(exc),
            )
            return None
        except ValueError as exc:
            await self._refuse(websocket, MALFORMED_HELLO, str(exc))
            return None
        origin = websocket.headers.get("origin")
        if not self.origin_allowed(origin):
            await self._refuse(
                websocket,
                ORIGIN_REFUSED,
                f"origin {origin!r} is not allowed on this server",
            )
            return None
        try:
            # Off the loop: a store behind a database blocks no other socket.
            return await asyncio.to_thread(self.authenticator.verify_hello, hello)
        except CredentialRefused as exc:
            await self._refuse(websocket, CREDENTIAL_REFUSED, str(exc))
            return None


class Connection:
    """One socket after its hello. The receive loop dispatches the frames. One
    sender task sends everything (the control queue and every session queue),
    so no two tasks write the socket."""

    def __init__(self, app, websocket, permissions):
        self.app = app
        self.websocket = websocket
        # The PermissionContext of the hello (see winslow.access.permissions).
        self.permissions = permissions
        self.wake = asyncio.Event()
        self.control = FrameQueue(wake=self.wake, maxlen=app.qsize)
        # session_id -> FrameQueue. The task log keys of a session live on its
        # queue, so removing the queue releases both (see EventBridge).
        self.queues = {}
        self.jobs = set()

    async def run(self):
        await self.websocket.send_text(
            encode_frame(
                HelloOkFrame(
                    user=self.permissions.user,
                    service=settings.SERVICE_NAME,
                    env=settings.ENV,
                    env_color=settings.env_color(),
                    scopes=wire_scopes(self.permissions.scopes),
                    credential=self.permissions.credential,
                    expires_at=self.permissions.expires_at,
                    credential_id=self.permissions.credential_id,
                )
            )
        )
        send_task = asyncio.get_running_loop().create_task(self._sender())
        expired = False
        try:
            # One scope for the socket: a job task copies the context when it
            # is spawned, a worker thread when it is called.
            async with until_expiry(self.permissions) as bound:
                with scoped_permissions(self.permissions):
                    await self._receive_loop()
            expired = bound.expired()
        finally:
            send_task.cancel()
            for job in self.jobs:
                job.cancel()
            for session_id, queue in self.queues.items():
                bridge = self.app.bridges.get(session_id)
                if bridge is not None:
                    bridge.remove_queue(queue)
        if expired:
            await self.close(CREDENTIAL_EXPIRED, EXPIRED_REASON)

    async def close(self, code, reason):
        """Close the socket once, after the sender is gone. A client that left
        in the same tick has closed already, and a second close would raise."""
        socket = self.websocket
        if (
            socket.application_state is WebSocketState.CONNECTED
            and socket.client_state is WebSocketState.CONNECTED
        ):
            await socket.close(code=code, reason=reason)

    async def _receive_loop(self):
        """Dispatch every inbound frame until the client disconnects."""
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
                    await self.close(CLIENT_TOO_SLOW, TOO_SLOW_REASON)
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

    def may_view(self, session, request_id):
        """A subscription is a transport concept, so the port decorators do
        not see it: the connection checks view_task on the session's scope
        name before a bridge attaches. False after an error reply."""
        reason = check((Permission.VIEW_TASK,), session.permission_scope)
        if reason is None:
            return True
        self.request_error(request_id, reason)
        return False

    @handles(SubscribeFrame)
    def handle_subscribe(self, envelope):
        """Attach, snapshot, and queue, synchronous on the loop, so no drain
        pass runs between the attach and the snapshot. A second subscribe of
        one session resets the queue and resends the snapshot. This is how a
        client recovers from a sequence gap."""
        session = self.resolve_for(envelope)
        if session is None:
            return
        if not self.may_view(session, envelope.request_id):
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
        if not self.may_view(session, envelope.request_id):
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
        """One action as an ack frame, or an error frame (see ServeApp.submit).
        The handler runs as the user of this socket (see Connection.run)."""
        try:
            spec = PORT_ACTIONS.get(envelope.action)
            if spec is None:
                raise ValueError(
                    f"{envelope.action!r} names no action. The actions are "
                    f"{sorted(PORT_ACTIONS)}."
                )
            action = spec.build(envelope.fields)
            ack = await self.app.submit(action, envelope.session_id)
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


def create_app(registry, authenticator, *, orchestrator, state_store, **kwargs):
    """The ASGI app of one serve process (see ServeApp)."""
    return ServeApp(
        registry,
        authenticator,
        orchestrator=orchestrator,
        state_store=state_store,
        **kwargs,
    ).starlette()
