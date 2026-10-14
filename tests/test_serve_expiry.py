"""The credential lifetime on a live socket: the server closes at the expiry
of the credential with its own code, and both clients reconnect with a fresh
one. A short expiry keeps the tests fast."""

import itertools
import time

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from winslow.access.backends import AuthBackend, StoredCredential
from winslow.access.credentials import CredentialEntry, CredentialFile
from winslow.conf import settings
from winslow.constants import Mode
from winslow.exceptions import CredentialRefused
from winslow.model import ConnectionEvent
from winslow.access.permissions import CredentialKind, Permission, PermissionContext
from winslow.protocol.frames import HelloFrame, HelloOkFrame, RequestFrame, ResultFrame
from winslow.serve import create_app
from winslow.protocol.closes import CREDENTIAL_EXPIRED, EXPIRED_REASON
from winslow.session import Session, SessionRegistry

from harness import (
    FixedCredential,
    credential_authenticator,
    identity_of,
    bare_orchestrator,
    build_workflow,
    fixed_authenticator,
    scratch_state_store,
)
from test_client_websocket import RemoteAppClient, ServedProcess, wait_for


def expiring(seconds):
    """A policy whose every hello carries a full context that expires in
    `seconds` from the hello."""
    return fixed_authenticator(
        PermissionContext.full("can", CredentialKind.TICKET, time.time() + seconds)
    )


class ExpiringOnce(FixedCredential):
    """A credential kind whose first hello expires in `seconds` and whose
    later hellos carry no expiry, so a reconnect test sees one cut."""

    def __init__(self, seconds=0.6):
        self.seconds = seconds
        self.hellos = itertools.count()

    def identify(self, token):
        expires_at = time.time() + self.seconds if next(self.hellos) == 0 else None
        return identity_of(
            PermissionContext.full("can", CredentialKind.TICKET, expires_at)
        )


class RenewedOnce(FixedCredential):
    """A server backend: `old` expires one second after its first connection
    request and is refused after that, `new` carries no expiry."""

    def __init__(self):
        self.cut = None

    def identify(self, token):
        if token == "new":
            return identity_of(PermissionContext.full("can", CredentialKind.OIDC, None))
        if self.cut is None:
            self.cut = time.time() + 1.0
            return identity_of(
                PermissionContext.full("can", CredentialKind.OIDC, self.cut)
            )
        raise CredentialRefused("token expired - log in again")


class Renewing(AuthBackend):
    """The client side backend AUTH_BACKENDS lists: its refresh answers `new`."""

    kind = CredentialKind.OIDC
    name = "oidc"

    @classmethod
    def refresh(cls, entry):
        return CredentialEntry(token="new", kind=cls.kind, user=entry.user)


def live(e2e_repo):
    workflow = build_workflow(e2e_repo, "my-workflow", Mode.TUI)
    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    registry = SessionRegistry()
    registry.register(session)
    return workflow, session, registry


def test_the_terminal_client_renews_an_expired_credential_on_the_reconnect(
    e2e_repo, tmp_path, monkeypatch
):
    """The wire asks its credential source on every open, so the reconnect
    after a 4402 carries the entry the backend refreshed (see StoredCredential)."""
    monkeypatch.setattr(settings, "AUTH_BACKENDS", [f"{__name__}.Renewing"])
    workflow, session, registry = live(e2e_repo)
    process = ServedProcess(
        registry, authenticator=credential_authenticator(RenewedOnce())
    ).start()
    store = CredentialFile(tmp_path / "credentials.toml")
    store.set(
        process.url,
        CredentialEntry(
            token="old", kind=CredentialKind.OIDC, expires_at=int(time.time()) + 1
        ),
    )
    client = RemoteAppClient(
        process.url, credential=StoredCredential(store, process.url)
    ).connect()
    try:
        events = []
        client.subscribe_connection(events.append)
        # The first context ends at the cut, the renewed one at the socket bound.
        assert client.permissions().expires_at < time.time() + 5
        wait_for(
            lambda: ConnectionEvent(connected=False, code=CREDENTIAL_EXPIRED) in events,
            "the expiry never dropped the connection with its code",
        )
        wait_for(
            lambda: events and events[-1] == ConnectionEvent(connected=True),
            "the client never reconnected with the renewed credential",
        )
        assert store.get(process.url).token == "new"
        assert client.permissions().expires_at > time.time() + 60
    finally:
        client.close()
        process.stop()


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
        registry, authenticator=credential_authenticator(ExpiringOnce())
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
