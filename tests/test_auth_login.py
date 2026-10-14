"""`winslow auth login`, `logout` and `status`: the credential file of a
person, and how connect resolves its credential from it."""

import io
import pathlib

import pytest

from winslow.access.backends import AuthBackend, refresh_expired
from winslow.access.cli import run
from winslow.access.credentials import (
    CredentialEntry,
    CredentialFile,
    credential_for,
    credential_key,
    default_path,
    remember_user,
)
from winslow.conf import settings
from winslow.exceptions import MisconfigurationError
from winslow.access.permissions import CredentialKind
from winslow.protocol.frames import HelloOkFrame

from test_auth_cli import parse


def store(tmp_path):
    return CredentialFile(tmp_path / "config" / "credentials.toml")


def hello(user, kind):
    return HelloOkFrame(
        user=user,
        service="winslow",
        env="local",
        env_color="blue",
        scopes=(),
        credential=kind,
        expires_at=None,
    )


OAUTH = CredentialEntry(
    token="eyJ.a.b",
    kind=CredentialKind.OIDC,
    refresh_token="r",
    expires_at=1757400000,
    scope="etl:run_task",
    client={"client_id": "c", "client_secret": "s", "issuer": "https://issuer"},
)


# --- the key and the file ----------------------------------------------------------


def test_the_default_file_follows_the_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert default_path() == str(tmp_path / "xdg" / "winslow" / "credentials.toml")
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert default_path() == str(
        tmp_path / "home" / ".config" / "winslow" / "credentials.toml"
    )
    monkeypatch.setattr(settings, "CREDENTIALS_FILE", None)
    assert CredentialFile().path == pathlib.Path(default_path())


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
    s.set("wss://a/", CredentialEntry(token="wsl_x", kind=CredentialKind.TOKEN))
    s.set("wss://b", OAUTH)
    fresh = store(tmp_path)
    assert fresh.get("wss://a") == CredentialEntry(
        token="wsl_x", kind=CredentialKind.TOKEN
    )
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
        run(parse("status"), store(tmp_path), [].append)


def test_an_entry_without_a_kind_stops_the_command(tmp_path):
    path = tmp_path / "config" / "credentials.toml"
    path.parent.mkdir()
    path.write_text('["wss://a"]\ntoken = "wsl_x"\n')
    with pytest.raises(MisconfigurationError, match="kind"):
        store(tmp_path).get("wss://a")


# --- connect ---------------------------------------------------------------------


def test_connect_reads_the_store(tmp_path):
    s = store(tmp_path)
    assert credential_for("wss://a", s) is None
    s.set("wss://a", CredentialEntry(token="wsl_x", kind=CredentialKind.TOKEN))
    assert credential_for("wss://a", s) == "wsl_x"


def test_connect_remembers_the_user_of_a_verified_credential(tmp_path):
    s = store(tmp_path)
    s.set("wss://a", CredentialEntry(token="wsl_x", kind=CredentialKind.TOKEN))
    remember_user(s, "wss://a", hello("local", CredentialKind.LOCAL))
    remember_user(s, "wss://a", hello("anonymous", CredentialKind.OPEN))
    assert s.get("wss://a").user is None
    remember_user(s, "wss://a", hello("can", CredentialKind.TOKEN))
    assert s.get("wss://a") == CredentialEntry(
        token="wsl_x", kind=CredentialKind.TOKEN, user="can"
    )
    remember_user(s, "wss://b", hello("can", CredentialKind.TICKET))
    assert s.get("wss://b") is None


# --- the commands ----------------------------------------------------------------


def test_login_stores_from_stdin_or_the_prompt(tmp_path, monkeypatch):
    s, out, err = store(tmp_path), [], []
    monkeypatch.setattr("sys.stdin", io.StringIO("wsl_piped\n"))
    assert (
        run(parse("login", "wss://etl-host", "--with-token"), s, out.append, err.append)
        == 0
    )
    assert out == ["stored the credential for wss://etl-host"]
    assert s.get("wss://etl-host") == CredentialEntry(
        token="wsl_piped", kind=CredentialKind.TOKEN
    )

    monkeypatch.setattr("getpass.getpass", lambda prompt: "wsl_typed")
    assert run(parse("login", "wss://etl-host/"), s, out.append, err.append) == 0
    assert s.get("wss://etl-host") == CredentialEntry(
        token="wsl_typed", kind=CredentialKind.TOKEN
    )

    monkeypatch.setattr("getpass.getpass", lambda prompt: "")
    assert run(parse("login", "wss://other"), s, out.append, err.append) == 1
    assert s.get("wss://other") is None
    assert "no token given" in err[-1]


def test_login_kind_picks_the_backend_and_refuses_an_unlisted_one(
    tmp_path, monkeypatch
):
    s, out, err = store(tmp_path), [], []
    monkeypatch.setattr(
        settings,
        "AUTH_BACKENDS",
        [
            "winslow.access.backends.TokenBackend",
            "winslow.access.backends.TicketBackend",
        ],
    )
    monkeypatch.setattr("getpass.getpass", lambda prompt: "a.b.c")
    assert (
        run(parse("login", "wss://t", "--kind", "ticket"), s, out.append, err.append)
        == 0
    )
    assert s.get("wss://t") == CredentialEntry(
        token="a.b.c", kind=CredentialKind.TICKET
    )
    assert (
        run(parse("login", "wss://t", "--kind", "okta"), s, out.append, err.append) == 1
    )
    assert "'okta' names no listed auth backend" in err[-1]
    assert "token, ticket" in err[-1]


def test_login_replaces_a_whole_entry(tmp_path, monkeypatch):
    s = store(tmp_path)
    s.set("wss://b", OAUTH)
    monkeypatch.setattr("getpass.getpass", lambda prompt: "wsl_new")
    assert run(parse("login", "wss://b"), s, [].append, [].append) == 0
    assert s.get("wss://b") == CredentialEntry(
        token="wsl_new", kind=CredentialKind.TOKEN
    )


def test_login_refuses_clear_text_without_insecure_and_other_schemes(
    tmp_path, monkeypatch
):
    s, out, err = store(tmp_path), [], []
    monkeypatch.setattr("getpass.getpass", lambda prompt: "wsl_x")
    assert run(parse("login", "ws://etl-host:8866"), s, out.append, err.append) == 1
    assert "clear text" in err[0] and "--insecure" in err[0]
    assert s.get("ws://etl-host:8866") is None
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
    s, out = store(tmp_path), []
    assert run(parse("status"), s, out.append) == 0
    assert out == ["no credentials stored"]
    s.set(
        "wss://a", CredentialEntry(token="wsl_x", kind=CredentialKind.TOKEN, user="can")
    )
    s.set("wss://b", OAUTH)
    s.set(
        "ws://127.0.0.1:8866",
        CredentialEntry(token="a.b.c", kind=CredentialKind.TICKET),
    )
    out.clear()
    assert run(parse("status"), s, out.append) == 0
    assert out == [
        "wss://a  can  token",
        "wss://b  -  oidc",
        "ws://127.0.0.1:8866  -  ticket",
    ]
    out.clear()
    assert run(parse("logout", "wss://a/"), s, out.append) == 0
    assert out == ["removed the credential for wss://a"]
    out.clear()
    assert run(parse("logout", "wss://a"), s, out.append) == 1
    assert out == ["no credential stored for wss://a"]


class StubOidc(AuthBackend):
    """A listed backend whose refresh answers a fresh entry for every call."""

    kind = CredentialKind.OIDC
    name = "oidc"

    @classmethod
    def refresh(cls, entry):
        return CredentialEntry(token="fresh", kind=cls.kind, user=entry.user)


def test_connect_refreshes_an_expired_entry_through_its_listed_backend(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "AUTH_BACKENDS", [f"{__name__}.StubOidc"])
    s = store(tmp_path)
    expired = CredentialEntry(
        token="old", kind=CredentialKind.OIDC, user="can", expires_at=1
    )
    s.set("wss://a", expired)
    refresh_expired(s, "wss://a")
    assert s.get("wss://a") == CredentialEntry(
        token="fresh", kind=CredentialKind.OIDC, user="can"
    )

    live = CredentialEntry(token="live", kind=CredentialKind.OIDC, expires_at=2**40)
    s.set("wss://b", live)
    refresh_expired(s, "wss://b")
    assert s.get("wss://b") == live

    token = CredentialEntry(token="wsl_x", kind=CredentialKind.TOKEN, expires_at=1)
    s.set("wss://c", token)
    refresh_expired(s, "wss://c")
    assert s.get("wss://c") == token
    refresh_expired(s, "wss://none")


class Okta(StubOidc):
    """One of two listed issuers: an entry is its own by the issuer stored on it."""

    issuer = "https://okta"

    @classmethod
    def owns(cls, entry):
        return entry.kind == cls.kind and entry.client["issuer"] == cls.issuer

    @classmethod
    def refresh(cls, entry):
        return CredentialEntry(
            token=f"fresh-{cls.issuer}", kind=cls.kind, client=entry.client
        )


class Google(Okta):
    issuer = "https://google"


def test_the_refresh_runs_through_the_backend_that_owns_the_entry(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        settings, "AUTH_BACKENDS", [f"{__name__}.Okta", f"{__name__}.Google"]
    )
    s = store(tmp_path)
    google = CredentialEntry(
        token="old",
        kind=CredentialKind.OIDC,
        expires_at=1,
        client={"issuer": "https://google"},
    )
    s.set("wss://a", google)
    refresh_expired(s, "wss://a")
    assert s.get("wss://a").token == "fresh-https://google"
    # No listed backend owns an entry of another issuer: it stays as it is.
    other = CredentialEntry(
        token="old",
        kind=CredentialKind.OIDC,
        expires_at=1,
        client={"issuer": "https://else"},
    )
    s.set("wss://b", other)
    refresh_expired(s, "wss://b")
    assert s.get("wss://b") == other
