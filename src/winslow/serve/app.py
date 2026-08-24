"""The serve ASGI app: the /ws endpoint with the hello handshake and the
event stream. After the hello a client subscribes to sessions; each subscribe
answers with the session snapshot, and the events of its bridge follow with
sequence numbers. One sender task per connection sends everything; a client
that stays behind a full frame window is disconnected (see serve-spec)."""

import asyncio
import json
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from winslow.serve.bridge import EventBridge, Subscription

PROTOCOL_VERSION = 1

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


def create_app(registry, credentials, hello_timeout=5.0, qsize=10_000):
    """The Starlette app of one serve process. registry holds the live
    sessions; credentials is the policy of the bind (see Credentials)."""

    bridges = Bridges(qsize)

    async def refuse(websocket, code, reason):
        await websocket.send_json({"type": "hello_error", "reason": reason})
        await websocket.close(code=code, reason=reason)

    async def handshake(websocket):
        """Returns the user, or None after a refusal."""
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), hello_timeout)
        except asyncio.TimeoutError:
            await refuse(
                websocket, HELLO_TIMEOUT, f"no hello within {hello_timeout:g}s"
            )
            return None
        try:
            hello = json.loads(raw)
            if hello.get("type") != "hello":
                raise ValueError
        except (ValueError, AttributeError):
            await refuse(
                websocket, MALFORMED_HELLO, "the first message must be a hello"
            )
            return None
        user, error = credentials.verify_hello(hello, websocket.headers.get("origin"))
        if error:
            await refuse(websocket, CREDENTIAL_REFUSED, error)
            return None
        return user

    async def sender(websocket, wake, subscriptions, control):
        while True:
            wake.clear()
            for subscription in (control, *subscriptions.values()):
                while subscription.deque:
                    await websocket.send_text(subscription.deque.popleft())
                if subscription.behind_a_full_window:
                    await websocket.close(
                        code=CLIENT_TOO_SLOW,
                        reason="the client stays behind a full frame window - "
                        "reconnect and subscribe for a fresh snapshot",
                    )
                    return
            await wake.wait()

    def handle_subscribe(session_id, subscriptions, wake, control):
        """Attach, snapshot, and queue - synchronous on the loop, so no drain
        pass lands between the attach and the snapshot. A second subscribe of
        one session resets the queue and resends the snapshot: that is the
        recovery of a client that saw a sequence gap."""
        session = registry.get(session_id)
        if session is None:
            control.push(
                json.dumps(
                    {
                        "type": "error",
                        "reason": f"session id {session_id!r} does not resolve to a "
                        f"live session - it ended, or it belongs to another process.",
                    }
                )
            )
            return
        bridge = bridges.get_or_create(session)
        subscription = subscriptions.get(session_id)
        if subscription is None:
            subscription = Subscription(wake=wake, maxlen=qsize)
            subscriptions[session_id] = subscription
        else:
            subscription.deque.clear()
            subscription.dropped = 0
        bridge.subscribe(subscription)
        subscription.push(json.dumps(bridge.snapshot()))

    def handle_frame(frame, subscriptions, wake, control):
        kind = frame.get("type")
        session_id = frame.get("session_id")
        if kind == "subscribe":
            handle_subscribe(session_id, subscriptions, wake, control)
        elif kind == "unsubscribe":
            subscription = subscriptions.pop(session_id, None)
            bridge = bridges.get(session_id)
            if subscription is not None and bridge is not None:
                bridge.unsubscribe(subscription)
            control.push(
                json.dumps({"type": "unsubscribed", "session_id": session_id})
            )
        else:
            control.push(
                json.dumps(
                    {
                        "type": "error",
                        "reason": f"unknown message type {kind!r} - this server "
                        f"speaks subscribe and unsubscribe.",
                    }
                )
            )

    async def ws(websocket):
        await websocket.accept()
        user = await handshake(websocket)
        if user is None:
            return
        await websocket.send_json(
            {"type": "hello_ok", "user": user, "version": PROTOCOL_VERSION}
        )
        await websocket.send_json(
            {
                "type": "snapshot",
                "seq": 0,
                "sessions": [session_row(s) for s in registry.sessions()],
            }
        )

        wake = asyncio.Event()
        control = Subscription(wake=wake, maxlen=qsize)
        subscriptions = {}
        send_task = asyncio.get_running_loop().create_task(
            sender(websocket, wake, subscriptions, control)
        )
        try:
            while True:
                try:
                    frame = await websocket.receive_json()
                except (WebSocketDisconnect, RuntimeError):
                    return
                except ValueError:
                    control.push(
                        json.dumps(
                            {"type": "error", "reason": "the message is not JSON"}
                        )
                    )
                    continue
                handle_frame(frame, subscriptions, wake, control)
        finally:
            send_task.cancel()
            for session_id, subscription in subscriptions.items():
                bridge = bridges.get(session_id)
                if bridge is not None:
                    bridge.unsubscribe(subscription)

    @asynccontextmanager
    async def lifespan(app):
        # The composition point: the MCP session manager mounts here later.
        try:
            yield
        finally:
            bridges.close()

    return Starlette(routes=[WebSocketRoute("/ws", ws)], lifespan=lifespan)
