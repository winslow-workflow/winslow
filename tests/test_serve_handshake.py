"""The serve slice-one contract, black-box through the Starlette test client:
the hello handshake, the refusal channels, the credential policy, and the
session snapshot. The refusal codes and the 5s timeout come from the spike
findings; the timeout here is short, so the timeout test stays fast."""

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from winslow.constants import Mode
from winslow.serve import Credentials, create_app, mint_ticket, verify_ticket
from winslow.serve.app import PROTOCOL_VERSION
from winslow.session import Session, SessionRegistry

from harness import build_workflow

SECRET = "test-secret"
TOKEN = "test-token"


def client(registry=None, credentials=None, hello_timeout=0.2):
    credentials = credentials or Credentials(
        token=TOKEN,
        ticket_secret=SECRET,
        allowed_origins=("http://ui.example",),
        require_credential=True,
    )
    app = create_app(registry or SessionRegistry(), credentials, hello_timeout)
    return TestClient(app)


def hello(ws, **fields):
    ws.send_json({"type": "hello", "version": PROTOCOL_VERSION, **fields})


def expect_refusal(ws, code, reason_part):
    error = ws.receive_json()
    assert error["type"] == "hello_error"
    assert reason_part in error["reason"]
    with pytest.raises(WebSocketDisconnect) as exc:
        ws.receive_json()
    assert exc.value.code == code
    assert reason_part in exc.value.reason


def test_a_ticket_reaches_hello_ok_and_the_snapshot():
    with client().websocket_connect("/ws") as ws:
        hello(ws, ticket=mint_ticket(SECRET, "can"))
        assert ws.receive_json() == {
            "type": "hello_ok",
            "user": "can",
            "version": PROTOCOL_VERSION,
        }
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"
        assert snapshot["seq"] == 0
        assert snapshot["sessions"] == []


def test_a_bearer_token_reaches_hello_ok():
    with client().websocket_connect("/ws") as ws:
        hello(ws, token=TOKEN)
        assert ws.receive_json()["user"] == "token-client"


def test_a_garbage_ticket_refuses_on_both_channels():
    with client().websocket_connect("/ws") as ws:
        hello(ws, ticket="garbage")
        expect_refusal(ws, 4401, "malformed ticket")


def test_an_expired_ticket_names_the_recovery():
    with client().websocket_connect("/ws") as ws:
        hello(ws, ticket=mint_ticket(SECRET, "can", ttl=-1))
        expect_refusal(ws, 4401, "fetch a fresh one")


def test_a_wrong_token_refuses():
    with client().websocket_connect("/ws") as ws:
        hello(ws, token="wrong")
        expect_refusal(ws, 4401, "bad bearer token")


def test_a_non_hello_first_message_refuses():
    with client().websocket_connect("/ws") as ws:
        ws.send_json({"type": "subscribe"})
        expect_refusal(ws, 4400, "must be a hello")


def test_silence_refuses_on_the_hello_timeout():
    with client(hello_timeout=0.05).websocket_connect("/ws") as ws:
        expect_refusal(ws, 4408, "no hello within")


def test_a_loopback_bind_needs_no_credential():
    credentials = Credentials(require_credential=False)
    with client(credentials=credentials).websocket_connect("/ws") as ws:
        hello(ws)
        assert ws.receive_json()["user"] == "local"


def test_a_disallowed_origin_refuses():
    with client().websocket_connect(
        "/ws", headers={"Origin": "http://evil.example"}
    ) as ws:
        hello(ws, ticket=mint_ticket(SECRET, "can"))
        expect_refusal(ws, 4401, "not allowed")


def test_an_allowed_origin_passes():
    with client().websocket_connect(
        "/ws", headers={"Origin": "http://ui.example"}
    ) as ws:
        hello(ws, ticket=mint_ticket(SECRET, "can"))
        assert ws.receive_json()["user"] == "can"


def test_the_snapshot_lists_the_registered_sessions(e2e_repo):
    workflow = build_workflow(e2e_repo, "my-workflow", Mode.HEADLESS)
    registry = SessionRegistry()
    registry.register(Session(workflow))
    with client(registry=registry).websocket_connect("/ws") as ws:
        hello(ws, token=TOKEN)
        ws.receive_json()
        (row,) = ws.receive_json()["sessions"]
        assert row["session_id"] == workflow.session_id
        assert row["status"] == "ACTIVE"


def test_ticket_helpers_round_trip():
    user, error = verify_ticket(SECRET, mint_ticket(SECRET, "can"))
    assert (user, error) == ("can", None)
    user, error = verify_ticket("other-secret", mint_ticket(SECRET, "can"))
    assert user is None and "signature" in error


def test_the_cli_parses_the_serve_subcommand():
    from winslow.orchestrator import Action, Orchestrator

    args = Orchestrator.get_base_parser().parse_args(["serve", "--port", "9000"])
    assert args.action is Action.SERVE
    assert args.host == "127.0.0.1"
    assert args.port == 9000
