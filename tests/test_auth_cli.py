"""`winslow auth`: the token commands over the listed token backend, the two
policy reads, and the parser that routes them."""

import pytest

from winslow.access.backends import token_backend
from winslow.access.cli import run
from winslow.access.commands import (
    check,
    show,
    token_create,
    token_revoke,
    token_rows,
)
from winslow.access.credentials import CredentialFile
from winslow.conf import settings
from winslow.exceptions import MisconfigurationError
from winslow.orchestrator import Command, Orchestrator, OrchestratorConfig

from test_access import POLICY


@pytest.fixture
def auth(tmp_path, monkeypatch):
    """The policy file and the token file the default backend reads, and the
    credential file `run` takes: (credential file, token backend)."""
    path = tmp_path / "policy.toml"
    path.write_text(POLICY)
    monkeypatch.setattr(settings, "AUTH_POLICY_FILE", str(path))
    monkeypatch.setattr(settings, "AUTH_TOKENS_FILE", str(tmp_path / "tokens"))
    return CredentialFile(tmp_path / "credentials.toml"), token_backend()


def parse(*argv):
    config, unknown = Orchestrator.get_base_parser().parse_known_args(
        ["auth", *argv], namespace=OrchestratorConfig()
    )
    assert unknown == []
    assert config.action is Command.AUTH
    return config


def test_create_prints_the_token_once_and_stores_the_hash(tmp_path, auth):
    s, backend = auth
    tokens = backend.tokens
    out = []
    assert run(parse("token", "create", "can"), s, out.append) == 0
    (token,) = out
    assert token.startswith("wsl_")
    (record,) = tokens.list("can")
    assert record.expires_at is None
    assert token not in (tmp_path / "tokens").read_text()


def test_create_notes_a_user_without_a_scope_beside_the_token(auth):
    s, _ = auth
    out, err = [], []
    assert run(parse("token", "create", "nobody"), s, out.append, err.append) == 0
    assert out[0].startswith("wsl_") and len(out) == 1
    assert err == ["nobody holds no scope - the token grants nothing until one exists."]
    out, err = [], []
    run(parse("token", "create", "can"), s, out.append, err.append)
    assert err == []


def test_create_refuses_a_name_with_a_space(auth):
    tokens = auth[1].tokens
    with pytest.raises(MisconfigurationError, match="'ci bot' is not a name"):
        token_create(tokens, "ci bot")
    assert tokens.list() == ()


def test_create_with_an_end_date_and_list(auth):
    s, backend = auth
    tokens = backend.tokens
    token_create(tokens, "ci-bot", expires="90d", now=0.0)
    ((token_id, user, created, expires, state),) = token_rows(tokens, now=1.0)
    assert (user, state) == ("ci-bot", "live")
    assert created.startswith("1970")
    assert expires != "-"
    out = []
    run(parse("token", "list", "ci-bot"), s, out.append)
    assert out[0].startswith(token_id)


def test_revoke_by_id_and_by_user(auth):
    s, backend = auth
    tokens = backend.tokens
    token_create(tokens, "can")
    token_create(tokens, "can")
    token_create(tokens, "ci-bot")
    (first, second) = tokens.list("can")
    assert token_revoke(tokens, token_id=first.token_id) == 1
    assert tokens.list("can") == (second,)
    out = []
    assert run(parse("token", "revoke", "--user", "can"), s, out.append) == 0
    assert out == ["revoked 1 token"]
    assert tokens.list("can") == ()
    assert run(parse("token", "revoke", "--token-id", "nope"), s, out.append) == 1


@pytest.mark.parametrize("argv", [(), ("--token-id", "a", "--user", "can")])
def test_revoke_needs_exactly_one_target(argv, capsys):
    with pytest.raises(SystemExit) as exit:
        parse("token", "revoke", *argv)
    assert exit.value.code == 2
    assert "(--token-id TOKEN_ID | --user USER)" in capsys.readouterr().err


def test_show_and_check_read_the_policy(auth):
    s, backend = auth
    token_create(backend.tokens, "lucas")
    assert show(backend, "lucas") == {
        "user": "lucas",
        "scopes": ["etl:check_task", "etl:start_workflow", "etl:view_task"],
        "tokens": 1,
    }
    assert check(backend, "lucas", "etl", "check_task") == (
        True,
        "lucas holds etl:check_task",
    )
    allowed, reason = check(backend, "lucas", "etl", "run_task")
    assert allowed is False
    assert (
        reason
        == "lucas lacks etl:run_task - the scopes cover etl:check_task, etl:start_workflow, etl:view_task"
    )
    out = []
    assert run(parse("check", "lucas", "etl", "run_task"), s, out.append) == 1
    assert run(parse("show", "guest"), s, out.append) == 0
    assert "scopes: -" in out
