"""`winslow auth login`, `logout` and `status`: the credential file of a
person, and how connect resolves its credential from it."""

import io

import pytest

from winslow.access.cli import Stores, run
from winslow.access.credentials import (
    CredentialEntry,
    CredentialFile,
    credential_for,
    credential_key,
    remember_user,
)
from winslow.exceptions import MisconfigurationError
from winslow.permissions import CredentialKind
from winslow.protocol.frames import HelloOkFrame

from test_auth_cli import parse


def store(tmp_path):
    return CredentialFile(tmp_path / "config" / "credentials.toml")


def stores(tmp_path):
    return Stores(policy=None, tokens=None, credentials=store(tmp_path))


def hello(user, kind):
    return HelloOkFrame(
        user=user,
        env="local",
        env_color="blue",
        scopes=(),
        credential=kind,
        expires_at=None,
    )


OAUTH = CredentialEntry(
    token="eyJ.a.b",
    refresh_token="r",
    expires_at=1757400000,
    scope="etl:run_task",
    client={"client_id": "c", "client_secret": "s", "issuer": "https://issuer"},
)


# --- the key and the file ----------------------------------------------------------


def test_the_key_spells_the_server_once():
    assert credential_key("WSS://ETL-Host:8866/") == "wss://etl-host:8866"
    assert credential_key("ws://host:8866/ws") == "ws://host:8866"
    assert credential_key("ws://host:8866/other") == "ws://host:8866/other"
    assert credential_key("ws://[::1]:8866") == "ws://[::1]:8866"
    assert credential_key("ws://host") != credential_key("wss://host")


def test_the_key_names_the_server_connect_reaches():
    from winslow.client.websocket import normalize_url

    for url in ("ws://host:8866", "ws://host:8866/", "ws://host:8866/ws"):
        assert (
            credential_key(normalize_url(url))
            == credential_key(url)
            == "ws://host:8866"
        )


def test_the_file_round_trips_with_private_modes(tmp_path):
    s = store(tmp_path)
    assert s.get("wss://a") is None
    assert s.entries() == ()
    s.set("wss://a/", CredentialEntry(token="wsl_x"))
    s.set("wss://b", OAUTH)
    fresh = store(tmp_path)
    assert fresh.get("wss://a") == CredentialEntry(token="wsl_x")
    assert fresh.get("wss://b") == OAUTH
    assert [key for key, _ in fresh.entries()] == ["wss://a", "wss://b"]
    assert s.path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "config").stat().st_mode & 0o777 == 0o700
    assert s.delete("wss://a") is True
    assert s.delete("wss://a") is False
    assert [key for key, _ in store(tmp_path).entries()] == ["wss://b"]


def test_a_field_outside_the_schema_and_a_broken_file_stop_the_command(tmp_path):
    path = tmp_path / "config" / "credentials.toml"
    path.parent.mkdir()
    path.write_text('["wss://a"]\ntoken = "x"\ncolour = "red"\n')
    with pytest.raises(MisconfigurationError, match="colour"):
        store(tmp_path).get("wss://a")
    path.write_text('["wss://a"\n')
    with pytest.raises(MisconfigurationError, match="credentials.toml"):
        run(parse("status"), stores(tmp_path), [].append)


def test_the_kind_of_an_entry():
    assert CredentialEntry(token="wsl_x").kind == "per-user token"
    assert CredentialEntry(token="a.b.c").kind == "ticket"
    assert OAUTH.kind == "oauth"


# --- connect ---------------------------------------------------------------------


def test_connect_resolves_the_variable_then_the_store(tmp_path):
    s = store(tmp_path)
    assert credential_for("wss://a", s, None) is None
    s.set("wss://a", CredentialEntry(token="wsl_x"))
    assert credential_for("wss://a", s, None) == "wsl_x"
    assert credential_for("wss://a", s, "") == "wsl_x"
    assert credential_for("wss://a", s, "wsl_env") == "wsl_env"


def test_connect_remembers_the_user_of_a_verified_credential(tmp_path):
    s = store(tmp_path)
    s.set("wss://a", CredentialEntry(token="wsl_x"))
    remember_user(s, "wss://a", hello("local", CredentialKind.LOCAL))
    remember_user(s, "wss://a", hello("anonymous", CredentialKind.OPEN))
    assert s.get("wss://a").user is None
    remember_user(s, "wss://a", hello("can", CredentialKind.TOKEN))
    assert s.get("wss://a") == CredentialEntry(token="wsl_x", user="can")
    remember_user(s, "wss://b", hello("can", CredentialKind.TICKET))
    assert s.get("wss://b") is None


# --- the commands ----------------------------------------------------------------


def test_login_stores_from_stdin_or_the_prompt(tmp_path, monkeypatch):
    s, out, err = stores(tmp_path), [], []
    monkeypatch.setattr("sys.stdin", io.StringIO("wsl_piped\n"))
    assert (
        run(parse("login", "wss://etl-host", "--with-token"), s, out.append, err.append)
        == 0
    )
    assert out == ["stored the credential for wss://etl-host"]
    assert s.credentials.get("wss://etl-host") == CredentialEntry(token="wsl_piped")

    monkeypatch.setattr("getpass.getpass", lambda prompt: "wsl_typed")
    assert run(parse("login", "wss://etl-host/"), s, out.append, err.append) == 0
    assert s.credentials.get("wss://etl-host") == CredentialEntry(token="wsl_typed")

    monkeypatch.setattr("getpass.getpass", lambda prompt: "")
    assert run(parse("login", "wss://other"), s, out.append, err.append) == 1
    assert s.credentials.get("wss://other") is None
    assert "no token given" in err[-1]


def test_login_replaces_a_whole_entry(tmp_path, monkeypatch):
    s = stores(tmp_path)
    s.credentials.set("wss://b", OAUTH)
    monkeypatch.setattr("getpass.getpass", lambda prompt: "wsl_new")
    assert run(parse("login", "wss://b"), s, [].append, [].append) == 0
    assert s.credentials.get("wss://b") == CredentialEntry(token="wsl_new")


def test_login_refuses_clear_text_without_insecure_and_other_schemes(
    tmp_path, monkeypatch
):
    s, out, err = stores(tmp_path), [], []
    monkeypatch.setattr("getpass.getpass", lambda prompt: "wsl_x")
    assert run(parse("login", "ws://etl-host:8866"), s, out.append, err.append) == 1
    assert "clear text" in err[0] and "--insecure" in err[0]
    assert s.credentials.get("ws://etl-host:8866") is None
    assert (
        run(
            parse("login", "ws://etl-host:8866", "--insecure"),
            s,
            out.append,
            err.append,
        )
        == 0
    )
    assert run(parse("login", "ws://127.0.0.1:8866"), s, out.append, err.append) == 0
    for url in ("ftp://host", "etl-host", "http://host"):
        assert run(parse("login", url), s, out.append, err.append) == 1
        assert "ws, wss" in err[-1]


def test_logout_and_status(tmp_path):
    s, out = stores(tmp_path), []
    assert run(parse("status"), s, out.append) == 0
    assert out == ["no credentials stored"]
    s.credentials.set("wss://a", CredentialEntry(token="wsl_x", user="can"))
    s.credentials.set("wss://b", OAUTH)
    s.credentials.set("ws://127.0.0.1:8866", CredentialEntry(token="a.b.c"))
    out.clear()
    assert run(parse("status"), s, out.append) == 0
    assert out == [
        "wss://a  can  per-user token",
        "wss://b  -  oauth",
        "ws://127.0.0.1:8866  -  ticket",
    ]
    out.clear()
    assert run(parse("logout", "wss://a/"), s, out.append) == 0
    assert out == ["removed the credential for wss://a"]
    out.clear()
    assert run(parse("logout", "wss://a"), s, out.append) == 1
    assert out == ["no credential stored for wss://a"]
