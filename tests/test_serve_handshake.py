"""The serve slice-one contract, black-box through the Starlette test client:
the hello handshake, the refusal channels, and the credential policy. The
refusal codes and the 5s timeout come from the spike
findings; the timeout here is short, so the timeout test stays fast."""

import asyncio
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from winslow import settings
from winslow.constants import Mode
from winslow.exceptions import CredentialRefused, MisconfigurationError
from winslow.permissions import ALL, expand, wire_scopes
from winslow.serve import Credentials, create_app, mint_ticket, verify_ticket
from winslow.serve.auth import TicketCredential
from winslow.serve.mcp import BearerTokenVerifier
from winslow.session import Session, SessionRegistry
from winslow.protocol.frames import RequestFrame
from winslow.protocol.frames import (
    HelloErrorFrame,
    HelloFrame,
    HelloOkFrame,
    ResultFrame,
    SubscribeFrame,
)

from harness import bare_orchestrator, build_workflow, scratch_state_store

SECRET = "test-secret-of-thirty-two-bytes-or-more"
AUDIENCE = "https://etl-host"


def ticket(user="can", scopes=("*:*",), ttl=900, secret=SECRET, audience=AUDIENCE):
    return mint_ticket(secret, user, scopes, audience, ttl=ttl)


def claims_of(token):
    return jwt.decode(token, options={"verify_signature": False})


def tickets_only():
    return Credentials(chain=(TicketCredential(SECRET, AUDIENCE),))


def client(registry=None, credentials=None, hello_timeout=0.2):
    app = create_app(
        registry or SessionRegistry(),
        credentials or tickets_only(),
        orchestrator=bare_orchestrator(),
        state_store=scratch_state_store(),
        hello_timeout=hello_timeout,
        allowed_origins=("http://ui.example",),
    )
    return TestClient(app)


def hello(ws, **fields):
    ws.send_json({"type": HelloFrame.type, **fields})


def expect_refusal(ws, code, reason_part):
    error = ws.receive_json()
    assert error["type"] == HelloErrorFrame.type
    assert reason_part in error["reason"]
    with pytest.raises(WebSocketDisconnect) as exc:
        ws.receive_json()
    assert exc.value.code == code
    assert reason_part in exc.value.reason


def test_a_ticket_reaches_hello_ok():
    minted = ticket()
    with client().websocket_connect("/ws") as ws:
        hello(ws, token=minted)
        assert ws.receive_json() == {
            "type": HelloOkFrame.type,
            "user": "can",
            "env": settings.env,
            "env_color": settings.ENV_COLOR,
            "scopes": list(wire_scopes(ALL)),
            "credential": "ticket",
            "expires_at": float(claims_of(minted)["exp"]),
        }


@pytest.mark.parametrize(
    "credentials, kind, user, credential",
    [
        (None, "ticket", "can", "ticket"),
        (Credentials(require_credential=False), None, "local", "local"),
    ],
    ids=["ticket", "local"],
)
def test_every_credential_reaches_hello_ok(credentials, kind, user, credential):
    """Each credential kind reaches hello_ok under its user and kind. A
    credential without an expiry of its own gets the bounded one."""
    fields = {"token": ticket()} if kind == "ticket" else {}
    before = time.time()
    with client(credentials=credentials).websocket_connect("/ws") as ws:
        hello(ws, **fields)
        frame = ws.receive_json()
    assert frame["user"] == user
    assert frame["credential"] == credential
    assert frame["scopes"] == list(wire_scopes(ALL))
    if credential != "ticket":
        assert frame["expires_at"] >= before + settings.AUTH_MAX_CONNECTION


def test_a_garbage_ticket_refuses_on_both_channels():
    with client().websocket_connect("/ws") as ws:
        hello(ws, token="x.y.z")
        expect_refusal(ws, 4401, "bad ticket")


def test_a_ticket_on_a_server_without_a_secret_names_the_accepted_kinds():
    bare = Credentials(require_credential=True)
    with client(credentials=bare).websocket_connect("/ws") as ws:
        hello(ws, token=ticket())
        expect_refusal(ws, 4401, "this server accepts no credential")


def test_an_expired_ticket_names_the_recovery():
    with client().websocket_connect("/ws") as ws:
        hello(ws, token=expired_ticket())
        expect_refusal(ws, 4401, "fetch a fresh one")


def test_a_wrong_token_refuses():
    with client().websocket_connect("/ws") as ws:
        hello(ws, token="wrong")
        expect_refusal(ws, 4401, "bad bearer token")


def test_a_non_hello_first_message_refuses():
    with client().websocket_connect("/ws") as ws:
        ws.send_json({"type": SubscribeFrame.type})
        expect_refusal(ws, 4400, "must be a hello")


def test_a_hello_with_a_wrong_field_type_refuses_as_malformed():
    with client().websocket_connect("/ws") as ws:
        hello(ws, token=123)
        error = ws.receive_json()
        assert error["type"] == HelloErrorFrame.type
        assert "malformed" in error["reason"]
        assert "token" in error["detail"]
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4400


def test_a_binary_first_message_refuses():
    with client().websocket_connect("/ws") as ws:
        ws.send_bytes(b"\x00\x01")
        expect_refusal(ws, 4400, "text message")


def test_silence_refuses_on_the_hello_timeout():
    with client(hello_timeout=0.05).websocket_connect("/ws") as ws:
        expect_refusal(ws, 4408, "no hello within")


def test_a_loopback_bind_needs_no_credential():
    credentials = Credentials(require_credential=False)
    with client(credentials=credentials).websocket_connect("/ws") as ws:
        hello(ws)
        assert ws.receive_json()["user"] == "local"


def test_a_loopback_bind_keeps_the_origin_rule():
    credentials = Credentials(require_credential=False)
    with client(credentials=credentials).websocket_connect(
        "/ws", headers={"Origin": "http://evil.example"}
    ) as ws:
        hello(ws)
        expect_refusal(ws, 4403, "not allowed")


def test_a_non_ascii_token_refuses_as_a_bad_token():
    with client().websocket_connect("/ws") as ws:
        hello(ws, token="tökén")
        expect_refusal(ws, 4401, "bad bearer token")


def test_a_disallowed_origin_refuses():
    with client().websocket_connect(
        "/ws", headers={"Origin": "http://evil.example"}
    ) as ws:
        hello(ws, token=ticket())
        expect_refusal(ws, 4403, "not allowed")


def test_an_allowed_origin_passes():
    with client().websocket_connect(
        "/ws", headers={"Origin": "http://ui.example"}
    ) as ws:
        hello(ws, token=ticket())
        assert ws.receive_json()["user"] == "can"


def test_the_sessions_read_lists_the_registered_sessions(e2e_repo):
    workflow = build_workflow(e2e_repo, "my-workflow", Mode.HEADLESS)
    registry = SessionRegistry()
    registry.register(Session(workflow))
    with client(registry=registry).websocket_connect("/ws") as ws:
        hello(ws, token=ticket())
        assert ws.receive_json()["type"] == HelloOkFrame.type
        ws.send_json(
            {"type": RequestFrame.type, "request_id": "r-1", "kind": "sessions"}
        )
        result = ws.receive_json()
        assert result["type"] == ResultFrame.type
        (row,) = result["result"]
        assert row["session_id"] == workflow.session_id
        assert row["status"] == "ACTIVE"


def test_a_ticket_round_trips_with_its_scopes():
    minted = ticket(scopes=("etl:run_task", "reports:view_task"))
    context = verify_ticket(SECRET, minted, AUDIENCE)
    assert (context.user, context.credential) == ("can", "ticket")
    assert context.scopes == expand(("etl:run_task", "reports:view_task"))
    assert context.expires_at == float(claims_of(minted)["exp"])
    assert set(claims_of(minted)) == {"sub", "scope", "aud", "iat", "exp", "jti"}


def tampered(token):
    head, body, signature = token.split(".")
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    return f"{head}.{body}.{flipped}"


def without(token, claim):
    claims = {k: v for k, v in claims_of(token).items() if k != claim}
    return jwt.encode(claims, SECRET, algorithm="HS256")


def with_claim(token, claim, value):
    return jwt.encode({**claims_of(token), claim: value}, SECRET, algorithm="HS256")


def expired_ticket(seconds_ago=60):
    """A ticket whose exp passed: mint refuses one, so it is written by hand."""
    minted = ticket()
    iat = claims_of(minted)["iat"] - 900 - seconds_ago
    return with_claim(with_claim(minted, "iat", iat), "exp", iat + 900)


def rs256_ticket():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return jwt.encode(claims_of(ticket()), key, algorithm="RS256")


@pytest.mark.parametrize(
    "bad, reason",
    [
        (lambda: tampered(ticket()), "Signature verification failed"),
        (expired_ticket, "ticket expired - fetch a fresh one and reconnect"),
        (
            lambda: ticket(secret="other-secret-of-thirty-two-bytes"),
            "Signature verification failed",
        ),
        (
            lambda: jwt.encode(claims_of(ticket()), None, algorithm="none"),
            "alg value is not allowed",
        ),
        (rs256_ticket, "alg value is not allowed"),
        (lambda: without(ticket(), "sub"), 'missing the "sub" claim'),
        (lambda: without(ticket(), "exp"), 'missing the "exp" claim'),
        (lambda: without(ticket(), "iat"), 'missing the "iat" claim'),
        (lambda: without(ticket(), "jti"), 'missing the "jti" claim'),
        (
            lambda: ticket(audience="https://other-host"),
            "ticket for another server - this one is https://etl-host",
        ),
        (lambda: with_claim(ticket(), "scope", "etl:fly"), "names no permission"),
        (lambda: with_claim(ticket(), "scope", 7), "scope is a space separated string"),
        (lambda: with_claim(ticket(), "scope", ""), "scope is empty"),
        (lambda: without(ticket(), "scope"), 'missing the "scope" claim'),
        (lambda: with_claim(ticket(), "sub", ""), "sub: "),
        (lambda: with_claim(ticket(), "sub", "ci bot"), "'ci bot' is not a name"),
        (
            lambda: with_claim(ticket(), "iat", -5),
            "iat: Input should be greater than or equal to 0",
        ),
        (
            lambda: with_claim(ticket(), "exp", claims_of(ticket())["iat"]),
            "exp is not after iat",
        ),
        (
            lambda: with_claim(ticket(), "role", "admin"),
            "role: Unexpected keyword argument",
        ),
        (
            lambda: with_claim(ticket(), "nbf", claims_of(ticket())["exp"] + 1),
            "not yet valid",
        ),
        (lambda: "garbage", "Not enough segments"),
        (
            lambda: with_claim(ticket(), "exp", claims_of(ticket())["iat"] + 7200),
            "ticket lives longer than 3600s",
        ),
    ],
    ids=[
        "tampered",
        "expired",
        "other-secret",
        "alg-none",
        "rs256",
        "no-sub",
        "no-exp",
        "no-iat",
        "no-jti",
        "other-aud",
        "bad-scope",
        "scope-number",
        "scope-empty",
        "no-scope",
        "sub-empty",
        "sub-space",
        "iat-negative",
        "exp-at-iat",
        "unknown-claim",
        "nbf-after-exp",
        "garbage",
        "too-long",
    ],
)
def test_a_bad_ticket_refuses_with_the_reason(bad, reason):
    with pytest.raises(CredentialRefused) as refused:
        verify_ticket(SECRET, bad(), AUDIENCE)
    assert reason in str(refused.value)


def test_mint_refuses_a_lifetime_over_the_bound():
    with pytest.raises(ValueError, match="longer than 3600s"):
        ticket(ttl=7200)
    assert (
        claims_of(ticket(ttl=3600))["exp"] - claims_of(ticket(ttl=3600))["iat"] == 3600
    )


def test_mint_applies_the_rules_of_the_verifier():
    with pytest.raises(ValueError, match="'etl:fly' names no permission"):
        ticket(scopes=("etl:fly",))
    with pytest.raises(ValueError, match="scope is empty"):
        ticket(scopes=())
    with pytest.raises(ValueError, match="'ci bot' is not a name"):
        ticket(user="ci bot")
    with pytest.raises(ValueError, match="exp is not after iat"):
        ticket(ttl=0)
    with pytest.raises(MisconfigurationError, match="shorter than 32 bytes"):
        ticket(secret="short")
    # One string of spellings is the wire form and mints as it is.
    assert claims_of(ticket(scopes="etl:run_task reports:view_task"))["scope"] == (
        "etl:run_task reports:view_task"
    )
    assert set(claims_of(ticket())) == {"sub", "scope", "aud", "iat", "exp", "jti"}
    assert claims_of(ticket())["jti"] != claims_of(ticket())["jti"]


def test_a_scope_list_verifies_like_the_string():
    listed = with_claim(ticket(), "scope", ["etl:run_task", "reports:view_task"])
    context = verify_ticket(SECRET, listed, AUDIENCE)
    assert context.scopes == expand(("etl:run_task", "reports:view_task"))


def test_a_ticket_from_a_clock_thirty_seconds_ahead_passes_on_the_leeway():
    ahead = claims_of(ticket())["iat"] + 30
    minted = with_claim(with_claim(ticket(), "iat", ahead), "exp", ahead + 900)
    assert verify_ticket(SECRET, minted, AUDIENCE).user == "can"
    far = with_claim(with_claim(ticket(), "iat", ahead + 90), "exp", ahead + 990)
    with pytest.raises(CredentialRefused, match="not yet valid"):
        verify_ticket(SECRET, far, AUDIENCE)


def test_a_ticket_builds_the_same_context_on_both_doors():
    minted = ticket(scopes=("etl:check_task",))
    credentials = tickets_only()
    by_token = credentials.verify_hello(HelloFrame(token=minted))
    access_token = asyncio.run(BearerTokenVerifier(credentials).verify_token(minted))
    by_header = credentials.mcp_context(access_token)
    assert by_token == by_header
    assert by_token.scopes == expand(("etl:check_task",))
    assert by_token.expires_at == float(claims_of(minted)["exp"])


def test_the_ticket_kind_refuses_a_secret_without_a_public_url_or_too_short():
    with pytest.raises(MisconfigurationError, match="WINSLOW_PUBLIC_URL"):
        TicketCredential(SECRET, None)
    with pytest.raises(MisconfigurationError, match="shorter than 32 bytes"):
        TicketCredential("short", AUDIENCE)


def test_a_ticket_secret_without_a_public_url_or_too_short_refuses_startup(monkeypatch):
    monkeypatch.setattr(settings, "SERVE_TICKET_SECRET", SECRET)
    monkeypatch.setattr(settings, "PUBLIC_URL", None)
    with pytest.raises(MisconfigurationError, match="WINSLOW_PUBLIC_URL"):
        Credentials.from_env("0.0.0.0")
    monkeypatch.setattr(settings, "PUBLIC_URL", AUDIENCE)
    assert Credentials.from_env("0.0.0.0").accepted == "per-user token, ticket"
    monkeypatch.setattr(settings, "SERVE_TICKET_SECRET", "short")
    with pytest.raises(MisconfigurationError, match="shorter than 32 bytes"):
        Credentials.from_env("0.0.0.0")


def test_the_cli_parses_the_serve_subcommand():
    from winslow.orchestrator import Command, Orchestrator

    args = Orchestrator.get_base_parser().parse_args(["serve", "--port", "9000"])
    assert args.action is Command.SERVE
    assert args.host == "127.0.0.1"
    assert args.port == 9000
