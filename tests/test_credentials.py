"""The credential chain of a serve process: per-user tokens from the stores
with the scopes of their user, the shared token behind them, open mode, and
the startup lines that name what a process accepts."""

import asyncio
import logging
import os
import time
from contextlib import contextmanager

import pytest
from starlette.testclient import TestClient

from winslow.access import FilePolicyStore, FileTokenStore
from winslow.access.cli import token_create, token_revoke
from winslow.constants import Mode
from winslow.exceptions import CredentialRefused
from winslow.permissions import CredentialKind, Permission
from winslow.protocol.codec import CODEC
from winslow.protocol.frames import (
    ActionFrame,
    AckFrame,
    HelloErrorFrame,
    HelloFrame,
    HelloOkFrame,
)
from winslow.serve import Credentials, create_app
from winslow.serve.auth import TokenCredential
from winslow.serve.mcp import BearerTokenVerifier
from winslow.session import Session, SessionRegistry

from harness import bare_orchestrator, build_workflow, by_name, scratch_state_store
from test_access import POLICY
from test_serve_mcp import call_tools, mcp_app


UNKNOWN_TOKEN = "unknown token - it was revoked or never issued"


def stores(tmp_path):
    path = tmp_path / "policy.toml"
    path.write_text(POLICY)
    return FilePolicyStore(path), FileTokenStore(tmp_path / "tokens")


def credentials(tmp_path, **overrides):
    policy, tokens = stores(tmp_path)
    chain = (TokenCredential(tokens, policy),)
    return Credentials(**{"chain": chain, **overrides}), tokens


def live(e2e_repo, name="my-workflow"):
    workflow = build_workflow(e2e_repo, name, Mode.TUI)
    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    registry = SessionRegistry()
    registry.register(session)
    return workflow, session, registry


@contextmanager
def hello(app, **fields):
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_json({"type": HelloFrame.type, **fields})
        yield ws, ws.receive_json()


# --- the chain ---------------------------------------------------------------------


def test_a_per_user_token_carries_the_scopes_of_its_user(tmp_path):
    policy, tokens = credentials(tmp_path)
    token = token_create(tokens, "lucas")
    context = policy.verify_bearer(token)
    assert (context.user, context.credential) == ("lucas", "token")
    assert context.allows("etl", Permission.CHECK_TASK)
    assert not context.allows("etl", Permission.RUN_TASK)
    assert context.expires_at is None


def test_an_unknown_token_and_an_expired_one_refuse(tmp_path):
    policy, tokens = credentials(tmp_path)
    with pytest.raises(CredentialRefused, match=UNKNOWN_TOKEN):
        policy.verify_bearer("wsl_nope")
    token = token_create(tokens, "can", expires="1s", now=time.time() - 5)
    with pytest.raises(CredentialRefused, match="token expired - ask for a new one"):
        policy.verify_bearer(token)


def test_a_token_of_no_known_shape_names_the_accepted_kinds(tmp_path):
    policy, _ = credentials(tmp_path)
    with pytest.raises(CredentialRefused, match="accepts per-user token$"):
        policy.verify_bearer("x")
    with pytest.raises(CredentialRefused, match="accepts no credential$"):
        Credentials().verify_bearer("x")


def test_the_websocket_door_caps_every_context_at_max_connection(tmp_path):
    policy, tokens = credentials(tmp_path, max_connection=60)
    before = time.time()
    context = policy.verify_hello(
        HelloFrame(token=token_create(tokens, "can", expires="90d"))
    )
    assert before + 60 <= context.expires_at <= time.time() + 60
    context = policy.verify_hello(
        HelloFrame(token=token_create(tokens, "can", expires="10s"))
    )
    assert context.expires_at <= time.time() + 10
    context = policy.verify_hello(HelloFrame(token=token_create(tokens, "can")))
    assert before + 60 <= context.expires_at <= time.time() + 60


def test_a_permission_change_reaches_the_next_hello(tmp_path, e2e_repo):
    """The policy reloads on its modification time; a live context keeps the
    scopes of its hello (see until_expiry for the bound)."""
    policy, tokens = credentials(tmp_path)
    workflow, session, registry = live(e2e_repo)
    token = token_create(tokens, "lucas")
    app = create_app(
        registry,
        policy,
        orchestrator=bare_orchestrator(),
        state_store=scratch_state_store(),
    )
    with hello(app, token=token) as (ws, hello_ok):
        assert hello_ok["user"] == "lucas"
        assert "etl:run_task" not in hello_ok["scopes"]

    path = tmp_path / "policy.toml"
    path.write_text(
        POLICY.replace(
            '[users.lucas]\ngroups = ["etl-checkers"]',
            '[users.lucas]\ngroups = ["etl-operators"]',
        )
    )
    stamp = path.stat().st_mtime + 2
    os.utime(path, (stamp, stamp))
    with hello(app, token=token) as (ws, hello_ok):
        assert "etl:run_task" in hello_ok["scopes"]


def test_a_revoked_token_refuses_the_next_hello_and_the_others_pass(tmp_path, e2e_repo):
    policy, tokens = credentials(tmp_path)
    _, _, registry = live(e2e_repo)
    first, second = token_create(tokens, "lucas"), token_create(tokens, "lucas")
    app = create_app(
        registry,
        policy,
        orchestrator=bare_orchestrator(),
        state_store=scratch_state_store(),
    )
    with hello(app, token=first) as (_, hello_ok):
        assert hello_ok["type"] == HelloOkFrame.type
    (record, _) = tokens.list("lucas")
    assert token_revoke(tokens, token_id=record.token_id) == 1
    with hello(app, token=first) as (_, refused):
        assert refused["type"] == HelloErrorFrame.type
        assert refused["reason"] == UNKNOWN_TOKEN
    with hello(app, token=second) as (_, hello_ok):
        assert hello_ok["user"] == "lucas"


def test_an_expired_token_refuses_the_hello(tmp_path, e2e_repo):
    policy, tokens = credentials(tmp_path)
    _, _, registry = live(e2e_repo)
    token = token_create(tokens, "lucas", expires="1s", now=time.time() - 5)
    app = create_app(
        registry,
        policy,
        orchestrator=bare_orchestrator(),
        state_store=scratch_state_store(),
    )
    with hello(app, token=token) as (_, refused):
        assert refused["type"] == HelloErrorFrame.type
        assert refused["reason"] == "token expired - ask for a new one"


def test_a_scoped_token_refuses_a_run_on_both_doors(tmp_path, e2e_repo):
    policy, tokens = credentials(tmp_path)
    workflow, session, registry = live(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    # my-workflow is the scope name; lucas holds etl alone, ci-bot holds nothing here.
    path = tmp_path / "policy.toml"
    path.write_text(POLICY.replace("etl:", "my-workflow:"))
    token = token_create(tokens, "lucas")

    app = create_app(
        registry,
        policy,
        orchestrator=bare_orchestrator(),
        state_store=scratch_state_store(),
    )
    with hello(app, token=token) as (ws, hello_ok):
        for action, accepted in (("check_tasks", True), ("run_tasks", False)):
            ws.send_json(
                {
                    "type": ActionFrame.type,
                    "request_id": action,
                    "session_id": session.session_id,
                    "action": action,
                    "fields": {"keys": [alpha.identity_key]},
                }
            )
            frame = ws.receive_json()
            while frame["type"] != AckFrame.type:
                frame = ws.receive_json()
            assert frame["ack"]["accepted"] is accepted, frame

    (ack,) = call_tools(
        mcp_app(registry, policy),
        [
            (
                "run_tasks",
                {"session_id": session.session_id, "keys": [alpha.identity_key]},
            )
        ],
        token=token,
    )
    assert ack["accepted"] is False
    assert "lucas lacks my-workflow:run_task" in ack["reason"]


# --- open mode ---------------------------------------------------------------------


def test_the_mcp_door_reads_the_stores_once_per_request(tmp_path):
    policy, tokens = credentials(tmp_path)
    token = token_create(tokens, "lucas", expires="90d")
    access_token = asyncio.run(BearerTokenVerifier(policy).verify_token(token))
    token_revoke(tokens, user="lucas")
    context = policy.mcp_context(access_token)
    assert (context.user, context.credential) == ("lucas", "token")
    assert context.allows("etl", Permission.CHECK_TASK)
    assert not context.allows("etl", Permission.RUN_TASK)
    # The SDK reads an int second for its own check; the context keeps its float.
    assert access_token.expires_at == int(context.expires_at)
    assert isinstance(context.expires_at, float)


def test_open_mode_passes_every_hello_and_names_a_known_token(tmp_path, e2e_repo):
    policy, tokens = credentials(tmp_path, open_mode=True)
    workflow, session, registry = live(e2e_repo)
    token = token_create(tokens, "lucas")
    app = create_app(
        registry,
        policy,
        orchestrator=bare_orchestrator(),
        state_store=scratch_state_store(),
        allowed_origins=("http://ui.example",),
    )
    with hello(app) as (ws, anonymous):
        # Decoded as the wire client decodes it: every door names a kind.
        frame = CODEC.decode(HelloOkFrame, anonymous)
        assert (frame.user, frame.credential) == ("anonymous", CredentialKind.OPEN)
        assert "*:run_task" in frame.scopes
    with hello(app, token=token) as (ws, named):
        assert (named["user"], named["credential"]) == ("lucas", "token")
        assert "*:run_task" in named["scopes"]
    # The Origin rule stays on.
    with TestClient(app).websocket_connect(
        "/ws", headers={"origin": "http://evil.example"}
    ) as ws:
        ws.send_json({"type": HelloFrame.type})
        assert ws.receive_json()["type"] == "hello_error"
    # The MCP door mounts without auth and answers as anonymous.
    (listed,) = call_tools(
        mcp_app(registry, policy), [("list_sessions", {})], token=None
    )
    row = listed[0] if isinstance(listed, list) else listed
    assert row["session_id"] == session.session_id


# --- the startup lines -------------------------------------------------------------


def test_describe_names_the_stores_the_credentials_and_the_warnings(tmp_path):
    policy, _ = credentials(tmp_path)
    levels = [level for level, _ in policy.describe("0.0.0.0:8866")]
    lines = "\n".join(line for _, line in policy.describe("0.0.0.0:8866"))
    assert levels == [logging.INFO, logging.INFO]
    assert "FilePolicyStore" in lines and "per-user token" in lines

    opened, _ = credentials(tmp_path, open_mode=True)
    assert opened.describe("0.0.0.0:8866")[-1][0] == logging.WARNING
    assert "open mode" in opened.describe("0.0.0.0:8866")[-1][1]


def test_describe_warns_about_a_policy_entry_that_grants_everything(tmp_path):
    # The credentials helper writes POLICY itself, so the admins group joins after it.
    admins = POLICY + '\n[groups.admins]\nscopes = ["*:*"]\n'
    policy, _ = credentials(tmp_path)
    (tmp_path / "policy.toml").write_text(admins)
    (warning,) = [
        line for level, line in policy.describe("b") if level == logging.WARNING
    ]
    assert warning == "auth: group 'admins' grants every permission on every workflow"
    opened, _ = credentials(tmp_path, open_mode=True)
    (tmp_path / "policy.toml").write_text(admins)
    assert "admins" not in "\n".join(line for _, line in opened.describe("b"))
