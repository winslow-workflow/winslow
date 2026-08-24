"""The serve ASGI app: the /ws endpoint with the hello handshake. The event
stream and the request layer land on this skeleton (see specs). The lifespan
is composed from day one: a mounted MCP app runs its session manager here."""

import asyncio
import json
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute

from winslow.session import SessionRegistry  # noqa: F401  (the app's contract)

PROTOCOL_VERSION = 1

# The refusal codes of the handshake. The refusal also rides a hello_error
# frame: after an accepted upgrade a browser reads the frame, the code, and
# the reason (see serve-spikes-findings, spike 1).
MALFORMED_HELLO = 4400
CREDENTIAL_REFUSED = 4401
HELLO_TIMEOUT = 4408


def session_row(session):
    return {
        "session_id": session.session_id,
        "workflow": str(session.workflow),
        "status": session.status.name,
    }


def create_app(registry, credentials, hello_timeout=5.0):
    """The Starlette app of one serve process. registry holds the live
    sessions; credentials is the policy of the bind (see Credentials)."""

    async def refuse(websocket, code, reason):
        await websocket.send_json({"type": "hello_error", "reason": reason})
        await websocket.close(code=code, reason=reason)

    async def ws(websocket):
        await websocket.accept()
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), hello_timeout)
        except asyncio.TimeoutError:
            await refuse(
                websocket, HELLO_TIMEOUT, f"no hello within {hello_timeout:g}s"
            )
            return

        try:
            hello = json.loads(raw)
            if hello.get("type") != "hello":
                raise ValueError
        except (ValueError, AttributeError):
            await refuse(
                websocket, MALFORMED_HELLO, "the first message must be a hello"
            )
            return

        user, error = credentials.verify_hello(
            hello, websocket.headers.get("origin")
        )
        if error:
            await refuse(websocket, CREDENTIAL_REFUSED, error)
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
        # The event stream and the request layer land here. Until then the
        # socket stays open and drains client frames.
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return

    @asynccontextmanager
    async def lifespan(app):
        # The composition point: the MCP session manager and the bridges of
        # the sessions run inside this scope.
        yield

    return Starlette(routes=[WebSocketRoute("/ws", ws)], lifespan=lifespan)
