"""The serve process: ServeApp owns the process state (the registry, the
credential policy, the bridges), Connection owns one socket after its hello
(the subscriptions, the control queue, the one sender task, the request
jobs). create_app builds the ASGI app from a ServeApp."""

import asyncio
import functools
import json
import traceback
from contextlib import asynccontextmanager
from dataclasses import asdict

from starlette.applications import Starlette
from starlette.routing import Mount, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from winslow.cache import declared_entries
from winslow.codec import CODEC, ValidationError
from winslow.exceptions import MisconfigurationError
from winslow.filter.builtin import enforce_builtin_only
from winslow.logger import INLINE_FORMATTER, LOGGER, get_task_dispatcher
from winslow.model import ActionFrame, SubscribeFrame, TaskLogSubscribeFrame
from winslow.serve.bridge import EventBridge, Subscription
from winslow.serve.wire import (
    INBOUND_FRAME_TYPES,
    REQUEST_CLASSES,
    FrameTypes,
    Requests,
    apply_filter_keys,
    build_action,
    cache_value_payload,
    caches_payload,
    descriptor_rows,
    history_rows,
    manifest_row,
    record_detail_payload,
    roster_payload,
    session_params_payload,
    session_row,
)
from winslow.session import create_session

PROTOCOL_VERSION = 1

# The refusal codes of the handshake. The refusal also rides a hello_error
# frame: after an accepted upgrade a browser reads the frame, the code, and
# the reason (see serve-spikes-findings, spike 1).
MALFORMED_HELLO = 4400
CREDENTIAL_REFUSED = 4401
HELLO_TIMEOUT = 4408
# The close code of a client that stays behind a full frame window.
CLIENT_TOO_SLOW = 1013


def request_handler(kind):
    """Mark a Connection method as the handler of one request kind (see
    Requests). run_request builds its dispatch table from every method this
    decorator tags, the same pattern the MCP tool registry uses (see
    winslow.serve.mcp.tool)."""

    def wrap(method):
        method._request_kind = kind
        return method

    return wrap


def requires_session(method):
    """Resolve the session before the method body runs, and pass it as a
    third argument. A session that does not resolve already answered the
    error frame (see Connection.resolve_for); the body never runs."""

    @functools.wraps(method)
    async def wrapper(self, envelope):
        session = self.resolve_for(envelope)
        if session is not None:
            await method(self, envelope, session)

    return wrapper


def requires_live_session(method):
    """requires_session, plus a refusal once the session has ended: its
    tasks and workflow cache are released (see Workflow.release_tasks), so
    a read past that point fails inside the handler with no direction.
    history, log_tail, record_detail, batch_options and session_params use
    requires_session instead - they read state that survives the release."""

    @requires_session
    async def guarded(self, envelope, session):
        if session.has_ended:
            self.request_error(
                envelope.request_id,
                f"{session.session_id} has ended - its live task and cache "
                f"state is released.",
            )
            return
        await method(self, envelope, session)

    # requires_session already wraps guarded with functools.wraps(guarded);
    # re-wrap with the real handler so a traceback names it, not "guarded".
    return functools.wraps(method)(guarded)


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
    alone; both share the registry and the credential policy. orchestrator
    and state_store power the descriptor and create_session requests; without
    them those requests answer with an error."""

    def __init__(
        self,
        registry,
        credentials,
        hello_timeout=5.0,
        qsize=10_000,
        orchestrator=None,
        state_store=None,
        ws=True,
        mcp=False,
        base_url="http://127.0.0.1:8866",
    ):
        self.registry = registry
        self.credentials = credentials
        self.hello_timeout = hello_timeout
        self.qsize = qsize
        self.orchestrator = orchestrator
        self.state_store = state_store
        self.bridges = Bridges(qsize)
        self.ws_enabled = ws
        self.base_url = base_url
        self.mcp_endpoint = self._build_mcp() if mcp else None
        if not ws and self.mcp_endpoint is None:
            raise MisconfigurationError(
                "The serve process needs at least one endpoint - "
                "enable the websocket, the MCP mount, or both."
            )

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
                # A mounted MCP app must be started by the parent lifespan
                # (see serve-spikes-findings, spike 3).
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

    async def _refuse(self, websocket, code, reason):
        await websocket.send_json({"type": FrameTypes.HELLO_ERROR, "reason": reason})
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
            if hello.get("type") != FrameTypes.HELLO:
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
        # (session_id, task_key) pairs this connection subscribed to (see
        # handle_subscribe_task_log). Cleaned up on disconnect.
        self.task_log_subscriptions = set()

    async def run(self):
        await self.websocket.send_json(
            {"type": FrameTypes.HELLO_OK, "user": self.user, "version": PROTOCOL_VERSION}
        )
        await self.websocket.send_json(
            {
                "type": FrameTypes.SNAPSHOT,
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
                    self.reply(
                        {"type": FrameTypes.ERROR, "reason": "the message is not JSON"}
                    )
                    continue
                if not isinstance(frame, dict):
                    self.reply(
                        {
                            "type": FrameTypes.ERROR,
                            "reason": f"a frame must be a JSON object, not "
                            f"{type(frame).__name__}.",
                        }
                    )
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
            for session_id, task_key in self.task_log_subscriptions:
                bridge = self.app.bridges.get(session_id)
                if bridge is not None:
                    bridge.unsubscribe_task_log(task_key)

    # --- the outgoing side ---------------------------------------------------

    def reply(self, payload):
        self.control.push(json.dumps(payload))

    def request_error(self, request_id, reason):
        self.reply(
            {"type": FrameTypes.ERROR, "request_id": request_id, "reason": reason}
        )

    def result(self, envelope, **payload):
        self.reply(
            {
                "type": FrameTypes.RESULT,
                "request_id": envelope.request_id,
                "kind": envelope.kind,
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
        match kind:
            case FrameTypes.SUBSCRIBE:
                if envelope := self.decode(frame, SubscribeFrame):
                    self.handle_subscribe(envelope)
            case FrameTypes.UNSUBSCRIBE:
                if envelope := self.decode(frame, SubscribeFrame):
                    self.handle_unsubscribe(envelope.session_id)
            case FrameTypes.SUBSCRIBE_TASK_LOG:
                if envelope := self.decode(frame, TaskLogSubscribeFrame):
                    self.handle_subscribe_task_log(envelope)
            case FrameTypes.UNSUBSCRIBE_TASK_LOG:
                if envelope := self.decode(frame, TaskLogSubscribeFrame):
                    self.handle_unsubscribe_task_log(envelope)
            case FrameTypes.ACTION:
                self.dispatch(frame, ActionFrame, self.run_action)
            case FrameTypes.REQUEST:
                self.dispatch_request(frame)
            case _:
                self.reply(
                    {
                        "type": FrameTypes.ERROR,
                        "reason": f"unknown message type {kind!r} - this "
                        f"server speaks {', '.join(INBOUND_FRAME_TYPES)}.",
                    }
                )

    def decode(self, frame, envelope_class):
        """The envelope of one inbound frame, or None after an error reply.
        Every inbound frame decodes through its envelope before a handler
        sees it: the envelope replaces a trusted frame.get(...) read (see
        winslow.model, winslow.codec)."""
        try:
            return CODEC.decode(envelope_class, frame)
        except ValidationError as exc:
            self.request_error(
                frame.get("request_id"),
                f"the {frame.get('type')} frame is malformed - {exc}",
            )
            return None

    def dispatch(self, frame, envelope_class, run):
        """Decode the frame, then spawn the handler as a job (see spawn):
        the async request and action path, where the handler itself may
        block or fail."""
        envelope = self.decode(frame, envelope_class)
        if envelope is not None:
            self.spawn(envelope, run(envelope))

    def dispatch_request(self, frame):
        """A request frame's envelope class depends on its kind (see
        REQUEST_CLASSES), so the kind is checked before the frame decodes -
        an unknown kind answers the same "names no request" reply the old
        flat envelope gave, instead of a validation error naming a missing
        "kind" field."""
        envelope_class = REQUEST_CLASSES.get(frame.get("kind"))
        if envelope_class is None:
            self.request_error(
                frame.get("request_id"),
                f"{frame.get('kind')!r} names no request. The requests are "
                f"{', '.join(sorted(REQUEST_CLASSES))}.",
            )
            return
        self.dispatch(frame, envelope_class, self.run_request)

    def spawn(self, envelope, coroutine):
        job = asyncio.get_running_loop().create_task(
            self._answered(envelope, coroutine)
        )
        self.jobs.add(job)
        job.add_done_callback(self.jobs.discard)

    async def _answered(self, envelope, coroutine):
        """No spawned job dies silent: the client reads an error frame
        instead of waiting on an answer that never comes."""
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
        session = self.app.registry.get(session_id)
        if session is None:
            LOGGER.debug(
                f"session id {session_id!r} (request {request_id!r}) does "
                f"not resolve to a live session."
            )
            self.request_error(
                request_id,
                f"session id {session_id!r} does not resolve to a live "
                f"session - it ended, or it belongs to another process.",
            )
        return session

    def resolve_for(self, envelope):
        return self.resolve(envelope.session_id, envelope.request_id)

    def handle_subscribe(self, envelope):
        """Attach, snapshot, and queue - synchronous on the loop, so no drain
        pass lands between the attach and the snapshot. A second subscribe of
        one session resets the queue and resends the snapshot: that is the
        recovery of a client that saw a sequence gap."""
        session = self.resolve_for(envelope)
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
        self.reply({"type": FrameTypes.UNSUBSCRIBED, "session_id": session_id})

    def handle_subscribe_task_log(self, envelope):
        """The backlog and the live stream of one task's log, outside any
        batch. The backlog answers at once. The live lines ride the session
        subscription as task_log_batch frames, so the client must already
        be subscribed to the session; this method never subscribes it."""
        session = self.resolve_for(envelope)
        if session is None:
            return
        if session.session_id not in self.subscriptions:
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
                f"{session.session_id} has ended - its live task state is "
                f"released.",
            )
            return
        task_key = envelope.task_key
        try:
            task = session.workflow.task_index.resolve(task_key)
        except KeyError as exc:
            self.request_error(envelope.request_id, exc.args[0])
            return
        bridge = self.app.bridges.get_or_create(session)
        key = (session.session_id, task_key)
        if key not in self.task_log_subscriptions:
            self.task_log_subscriptions.add(key)
            bridge.subscribe_task_log(task_key, task.log_key)
        backlog = get_task_dispatcher().buffered(task.log_key)
        self.reply(
            {
                "type": FrameTypes.TASK_LOG_BACKLOG,
                "session_id": session.session_id,
                "task_key": task_key,
                "lines": [INLINE_FORMATTER.format(record) for record in backlog],
            }
        )

    def handle_unsubscribe_task_log(self, envelope):
        session_id = envelope.session_id
        task_key = envelope.task_key
        key = (session_id, task_key)
        if key in self.task_log_subscriptions:
            self.task_log_subscriptions.discard(key)
            bridge = self.app.bridges.get(session_id)
            if bridge is not None:
                bridge.unsubscribe_task_log(task_key)
        self.reply(
            {
                "type": FrameTypes.UNSUBSCRIBED_TASK_LOG,
                "session_id": session_id,
                "task_key": task_key,
            }
        )

    async def run_action(self, envelope):
        session = self.resolve_for(envelope)
        if session is None:
            return
        try:
            action = build_action(envelope.action, envelope.fields)
        except ValueError as exc:
            self.request_error(envelope.request_id, str(exc))
            return
        # The admission gate can block: the submit runs on a worker thread.
        ack = await asyncio.to_thread(session.actions.submit_guarded, action)
        self.reply(
            {"type": FrameTypes.ACK, "request_id": envelope.request_id, **asdict(ack)}
        )

    async def run_request(self, envelope):
        # dispatch_request already resolved the kind to this envelope's
        # class, and every request class maps to a handler below.
        handler = self._request_handlers[envelope.kind]
        await handler(self, envelope)

    def _root_dir(self):
        return (
            getattr(self.app.orchestrator.orchestrator_config, "directory", None)
            if self.app.orchestrator is not None
            else None
        )

    @request_handler(Requests.DESCRIPTORS)
    async def _request_descriptors(self, envelope):
        if self.app.orchestrator is None:
            self.request_error(envelope.request_id, "this server serves no workflows")
            return
        self.result(envelope, **descriptor_rows(self.app.orchestrator))

    @request_handler(Requests.CREATE_SESSION)
    async def _request_create_session(self, envelope):
        if self.app.orchestrator is None or self.app.state_store is None:
            self.request_error(envelope.request_id, "this server creates no sessions")
            return
        try:
            session = await asyncio.to_thread(
                create_session,
                self.app.orchestrator,
                self.app.state_store,
                self.app.registry,
                envelope.workflow,
                envelope.overrides,
                envelope.values,
            )
        except Exception as exc:
            self.reply(
                {
                    "type": FrameTypes.ERROR,
                    "request_id": envelope.request_id,
                    "reason": str(exc.args[0] if exc.args else exc),
                    "detail": traceback.format_exc(),
                }
            )
            return
        self.result(envelope, **session_row(session))

    @request_handler(Requests.HISTORY)
    @requires_session
    async def _request_history(self, envelope, session):
        self.result(envelope, batches=history_rows(session))

    @request_handler(Requests.LOG_TAIL)
    @requires_session
    async def _request_log_tail(self, envelope, session):
        store = session.workflow.runner.record_store(envelope.batch_uuid)
        if store is None:
            self.request_error(
                envelope.request_id,
                f"batch {envelope.batch_uuid!r} keeps no records in this "
                f"session.",
            )
            return
        try:
            record = store.get_record(envelope.task_key)
        except KeyError:
            self.request_error(
                envelope.request_id,
                f"task {envelope.task_key!r} is not in the roster of "
                f"batch {envelope.batch_uuid!r}.",
            )
            return
        limit = envelope.limit or 200
        self.result(
            envelope,
            task_key=envelope.task_key,
            batch_uuid=envelope.batch_uuid,
            lines=record.log_tail(limit),
        )

    @request_handler(Requests.TASK_DETAIL)
    @requires_live_session
    async def _request_task_detail(self, envelope, session):
        try:
            task = session.workflow.task_index.resolve(envelope.task_key)
        except KeyError as exc:
            self.request_error(envelope.request_id, exc.args[0])
            return
        # The full capture evaluates user code: a worker thread runs it. The
        # session's task_info fills checked_at and effective_ttl from the
        # snapshots and evaluates cold descriptors, matching the local TUI
        # detail view.
        info = await asyncio.to_thread(
            session.workflow.task_info,
            task,
            full=True,
            evaluate=True,
            root_dir=self._root_dir(),
        )
        self.result(envelope, info=asdict(info))

    @request_handler(Requests.ROSTER)
    @requires_live_session
    async def _request_roster(self, envelope, session):
        payload = await asyncio.to_thread(roster_payload, session.workflow)
        self.result(envelope, **payload)

    @request_handler(Requests.CACHES)
    @requires_live_session
    async def _request_caches(self, envelope, session):
        payload = await asyncio.to_thread(caches_payload, session.workflow)
        self.result(envelope, **payload)

    @request_handler(Requests.CACHE_VALUE)
    @requires_live_session
    async def _request_cache_value(self, envelope, session):
        cache = session.workflow.get_cache(envelope.cache_name)
        if cache is None:
            self.request_error(
                envelope.request_id,
                f"{envelope.cache_name!r} names no cache of this session.",
            )
            return
        if envelope.entry_name not in declared_entries(type(cache)):
            self.request_error(
                envelope.request_id,
                f"{cache} has no entry {envelope.entry_name!r}.",
            )
            return
        payload = await asyncio.to_thread(
            cache_value_payload, cache, envelope.entry_name
        )
        self.result(envelope, **payload)

    @request_handler(Requests.RECORD_DETAIL)
    @requires_session
    async def _request_record_detail(self, envelope, session):
        store = session.workflow.runner.record_store(envelope.batch_uuid)
        if store is None:
            self.request_error(
                envelope.request_id,
                f"batch {envelope.batch_uuid!r} keeps no records in this "
                f"session.",
            )
            return
        try:
            record = store.get_record(envelope.task_key)
        except KeyError:
            self.request_error(
                envelope.request_id,
                f"task {envelope.task_key!r} is not in the roster of "
                f"batch {envelope.batch_uuid!r}.",
            )
            return
        self.result(envelope, **record_detail_payload(record))

    @request_handler(Requests.BATCH_OPTIONS)
    @requires_session
    async def _request_batch_options(self, envelope, session):
        self.result(envelope, options=asdict(session.workflow.batch_options))

    @request_handler(Requests.SESSION_PARAMS)
    @requires_session
    async def _request_session_params(self, envelope, session):
        self.result(envelope, **session_params_payload(session.workflow))

    @request_handler(Requests.APPLY_FILTER)
    @requires_live_session
    async def _request_apply_filter(self, envelope, session):
        try:
            query = session.workflow.filter_registry.parse(envelope.query)
            if envelope.builtin_only:
                enforce_builtin_only(query)
        except ValueError as exc:
            self.request_error(envelope.request_id, str(exc))
            return
        keys = await asyncio.to_thread(
            apply_filter_keys, query, session.workflow.tasks
        )
        self.result(envelope, keys=keys)

    @request_handler(Requests.MANIFESTS)
    async def _request_manifests(self, envelope):
        if self.app.state_store is None:
            self.request_error(
                envelope.request_id, "this server keeps no session state"
            )
            return
        manifests = await asyncio.to_thread(self.app.state_store.list_open_manifests)
        self.result(
            envelope,
            manifests=[
                manifest_row(m)
                for m in manifests
                if m.session_id not in self.app.registry
            ],
        )

    @request_handler(Requests.RESTORE_SESSION)
    async def _request_restore_session(self, envelope):
        if self.app.orchestrator is None or self.app.state_store is None:
            self.request_error(envelope.request_id, "this server creates no sessions")
            return
        if envelope.session_id in self.app.registry:
            self.request_error(
                envelope.request_id,
                f"{envelope.session_id!r} is already a live session.",
            )
            return
        manifest = next(
            (
                m
                for m in self.app.state_store.list_open_manifests()
                if m.session_id == envelope.session_id
            ),
            None,
        )
        if manifest is None:
            self.request_error(
                envelope.request_id,
                f"{envelope.session_id!r} names no open manifest to restore.",
            )
            return
        if manifest.workflow_class not in self.app.orchestrator.workflow_registry.names:
            self.request_error(
                envelope.request_id,
                f"the manifest names workflow {manifest.workflow_class!r}, "
                f"which this server does not collect.",
            )
            return
        try:
            session = await asyncio.to_thread(
                create_session,
                self.app.orchestrator,
                self.app.state_store,
                self.app.registry,
                manifest.workflow_class,
                manifest.orchestrator_overrides or {},
                manifest.workflow_values or {},
                manifest.session_id,
                True,
            )
        except Exception as exc:
            self.reply(
                {
                    "type": FrameTypes.ERROR,
                    "request_id": envelope.request_id,
                    "reason": str(exc.args[0] if exc.args else exc),
                    "detail": traceback.format_exc(),
                }
            )
            return
        self.result(envelope, **session_row(session))


# The dispatch table of run_request, built once from every method
# @request_handler tagged: adding a request means one method, tagged where
# it is declared, and nothing to keep in sync elsewhere.
Connection._request_handlers = {
    method._request_kind: method
    for method in vars(Connection).values()
    if hasattr(method, "_request_kind")
}


def create_app(registry, credentials, hello_timeout=5.0, qsize=10_000, **kwargs):
    """The ASGI app of one serve process (see ServeApp)."""
    return ServeApp(
        registry, credentials, hello_timeout=hello_timeout, qsize=qsize, **kwargs
    ).starlette()
