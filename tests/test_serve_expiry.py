"""The credential lifetime on a live socket: the server closes at the expiry
of the credential with its own code, and both clients reconnect with a fresh
one. A short expiry keeps the tests fast."""

import itertools
import time

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from winslow.constants import Mode
from winslow.model import ConnectionEvent
from winslow.permissions import CredentialKind, Permission, PermissionContext
from winslow.protocol.frames import HelloFrame, HelloOkFrame, RequestFrame, ResultFrame
from winslow.serve import Credentials, create_app
from winslow.protocol.closes import CREDENTIAL_EXPIRED, EXPIRED_REASON
from winslow.session import Session, SessionRegistry

from harness import (
    FixedCredential,
    bare_orchestrator,
    build_workflow,
    fixed_credentials,
    scratch_state_store,
)
from test_client_websocket import RemoteAppClient, ServedProcess, wait_for


def expiring(seconds):
    """A policy whose every hello carries a full context that expires in
    `seconds` from the hello."""
    return fixed_credentials(
        PermissionContext.full("can", CredentialKind.TICKET, time.time() + seconds)
    )


class ExpiringOnce(FixedCredential):
    """A credential kind whose first hello expires in `seconds` and whose
    later hellos carry no expiry, so a reconnect test sees one cut."""

    def __init__(self, seconds=0.6):
        self.seconds = seconds
        self.hellos = itertools.count()

    def verify(self, token):
        expires_at = time.time() + self.seconds if next(self.hellos) == 0 else None
        return PermissionContext.full("can", CredentialKind.TICKET, expires_at)


def live(e2e_repo):
    workflow = build_workflow(e2e_repo, "my-workflow", Mode.TUI)
    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    registry = SessionRegistry()
    registry.register(session)
    return workflow, session, registry


def test_remaining_is_the_seconds_to_the_expiry():
    def context(expires_at):
        return PermissionContext.full("can", CredentialKind.TICKET, expires_at)

    assert context(None).remaining() is None
    assert context(110.0).remaining(now=100.0) == 10.0
    assert context(90.0).remaining(now=100.0) == 0.0


def test_the_socket_closes_at_the_expiry_with_its_code(e2e_repo):
    workflow, session, registry = live(e2e_repo)
    app = create_app(
        registry,
        expiring(0.3),
        orchestrator=bare_orchestrator(),
        state_store=scratch_state_store(),
    )
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_json({"type": HelloFrame.type, "token": "t"})
        hello_ok = ws.receive_json()
        assert hello_ok["type"] == HelloOkFrame.type
        # The socket serves until the expiry.
        ws.send_json(
            {"type": RequestFrame.type, "request_id": "r-1", "kind": "sessions"}
        )
        assert ws.receive_json()["type"] == ResultFrame.type
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == CREDENTIAL_EXPIRED
    assert closed.value.reason == EXPIRED_REASON


def test_the_terminal_client_reconnects_after_the_expiry(e2e_repo):
    workflow, session, registry = live(e2e_repo)
    process = ServedProcess(
        registry, credentials=Credentials(chain=(ExpiringOnce(),))
    ).start()
    client = RemoteAppClient(process.url, token="t").connect()
    try:
        remote = client.session(session.session_id)
        events = []
        client.subscribe_connection(events.append)
        assert remote.snapshot().session_id == session.session_id
        # The drop carries the planned close code, so a client shows no warning.
        wait_for(
            lambda: ConnectionEvent(connected=False, code=CREDENTIAL_EXPIRED) in events,
            "the expiry never dropped the connection with its code",
        )
        wait_for(
            lambda: events and events[-1] == ConnectionEvent(connected=True),
            "the client never reconnected after the expiry",
        )
        # The new hello carries a fresh context, and the lane serves again.
        assert client.permissions().allows("my-workflow", Permission.RUN_TASK)
        assert remote.snapshot().session_id == session.session_id
    finally:
        client.close()
        process.stop()
