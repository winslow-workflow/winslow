"""The OpenID Connect backend: a token of an issuer verified against its key
set, the user and the groups from the claims, the refusals, and the login of
a person through the browser flow with its two seams replaced."""

import io
import threading
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from winslow.access.credentials import CredentialEntry
from winslow.access.file import FilePolicyStore
from winslow.access.permissions import CredentialKind, Permission
from winslow.conf import settings
from winslow.contrib.oidc import BrowserLogin, OidcBackend, pkce
from winslow.exceptions import CredentialRefused, MisconfigurationError
from winslow.access.backends import BackendChain, TokenBackend
from winslow.serve.auth import Authenticator

from test_access import POLICY

ISSUER = "https://issuer.example/oauth2/default"
AUDIENCE = "https://etl-host"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE = KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)
PUBLIC = KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
)


class Keys:
    """The key client of the tests: every token signs with the one key."""

    def get_signing_key_from_jwt(self, token):
        if jwt.get_unverified_header(token).get("kid") != "k1":
            raise jwt.PyJWKClientError("Unable to find a signing key that matches")
        return SimpleNamespace(key=PUBLIC)


def minted(**claims):
    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "can@b2c2.com",
        "iat": now,
        "exp": now + 600,
        "jti": "j-1",
        "cid": "client-a",
        **claims,
    }
    return jwt.encode(payload, PRIVATE, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture(autouse=True)
def public_url(monkeypatch):
    """The audience of every test token is the SERVE_PUBLIC_URL of the server."""
    monkeypatch.setattr(settings, "SERVE_PUBLIC_URL", AUDIENCE)


def backend(tmp_path, **options):
    path = tmp_path / "policy.toml"
    path.write_text(POLICY)
    return OidcBackend(
        ISSUER, AUDIENCE, policy=FilePolicyStore(path), key_client=Keys(), **options
    )


def verified(backend, token):
    """The context a server builds of the token (see BackendChain.context)."""
    return BackendChain((backend,)).context(token)


def test_the_mcp_metadata_names_the_issuer_as_the_authorization_server(tmp_path):
    """An agent reads where to log in from the protected resource metadata:
    the issuer of the listed OIDC backend, else the server itself."""
    from winslow.serve.mcp import McpEndpoint
    from harness import credential_authenticator

    class Endpoint(McpEndpoint):
        """The auth settings alone, without the server build."""

        def __init__(self, authenticator):
            self.serve_app = SimpleNamespace(authenticator=authenticator)

    oidc = Endpoint(credential_authenticator(backend(tmp_path)))._auth_settings(
        "http://etl-host:8866"
    )
    assert str(oidc["auth"].issuer_url) == ISSUER
    plain = Endpoint(credential_authenticator(TokenBackend()))._auth_settings(
        "http://etl-host:8866"
    )
    assert str(plain["auth"].issuer_url) == "http://etl-host:8866/"


# --- verify ------------------------------------------------------------------------


def test_a_token_names_its_user_its_groups_and_its_id(tmp_path):
    context = verified(
        backend(tmp_path), minted(sub="eve", groups=["WinslowProdViewers"])
    )
    assert (context.user, context.credential, context.credential_id) == (
        "eve",
        CredentialKind.OIDC,
        "j-1",
    )
    # eve has no policy entry: the groups the token asserts grant the scopes.
    assert context.allows("etl", Permission.VIEW_TASK)
    assert not context.allows("etl", Permission.RUN_TASK)
    assert context.expires_at == float(
        jwt.decode(minted(), options={"verify_signature": False})["exp"]
    )


def test_a_groups_claim_sent_as_one_string_is_one_group(tmp_path):
    context = verified(
        backend(tmp_path), minted(sub="eve", groups="WinslowProdViewers")
    )
    assert context.allows("etl", Permission.VIEW_TASK)
    context = verified(backend(tmp_path), minted(sub="eve", groups="admins"))
    assert not context.allows("etl", Permission.VIEW_TASK)


def test_a_listed_user_keeps_the_scopes_of_the_policy(tmp_path):
    context = verified(backend(tmp_path), minted(sub="can"))
    assert context.allows("etl", Permission.RUN_TASK)


@pytest.mark.parametrize(
    "claims, reason",
    [
        ({"iss": "https://other"}, "bad bearer token"),
        ({"aud": "https://other"}, "another audience"),
        ({"exp": int(time.time()) - 600}, "token expired"),
        ({"sub": ""}, "user: String should have at least 1 character"),
        ({"groups": [1]}, "groups.0: Input should be a valid string"),
    ],
)
def test_the_refusals_name_the_reason(tmp_path, claims, reason):
    with pytest.raises(CredentialRefused, match=reason):
        verified(backend(tmp_path), minted(**claims))


def test_a_missing_exp_refuses(tmp_path):
    now = int(time.time())
    token = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "iat": now},
        PRIVATE,
        algorithm="RS256",
        headers={"kid": "k1"},
    )
    with pytest.raises(CredentialRefused, match="bad token"):
        verified(backend(tmp_path), token)


def test_an_unknown_key_id_refuses_without_a_fetch(tmp_path):
    token = jwt.encode(
        jwt.decode(minted(), options={"verify_signature": False}),
        PRIVATE,
        algorithm="RS256",
        headers={"kid": "rotated"},
    )
    with pytest.raises(CredentialRefused, match="unknown key"):
        verified(backend(tmp_path), token)


def test_the_client_allow_list_and_the_allowed_domains(tmp_path):
    strict = backend(tmp_path, client_ids=("client-a",), allowed_domains=("b2c2.com",))
    assert verified(strict, minted()).user == "can@b2c2.com"
    with pytest.raises(CredentialRefused, match="token of client 'client-b'"):
        verified(strict, minted(cid="client-b"))
    with pytest.raises(CredentialRefused, match="token of client None"):
        verified(strict, minted(cid=None))
    with pytest.raises(CredentialRefused, match="not a login at b2c2.com"):
        verified(strict, minted(sub="can@evil.example"))
    with pytest.raises(CredentialRefused, match="not a login at b2c2.com"):
        verified(strict, minted(sub="@b2c2.com"))


def test_accepts_a_jwt_of_its_issuer_alone(tmp_path):
    oidc = backend(tmp_path)
    assert oidc.accepts(minted())
    assert not oidc.accepts(minted(iss="https://other"))
    assert not oidc.accepts("wsl_abc")
    assert not oidc.accepts("a.b.c")


# --- the settings ------------------------------------------------------------------


def test_the_defaults_need_an_issuer_and_isolate_a_shared_audience(monkeypatch):
    with pytest.raises(MisconfigurationError, match="set OIDC_ISSUER"):
        OidcBackend()
    monkeypatch.setattr(settings, "OIDC_ISSUER", ISSUER)

    class SharedAudience(OidcBackend):
        audience = "api://default"

    with pytest.raises(MisconfigurationError, match="set OIDC_CLIENT_IDS"):
        SharedAudience()
    monkeypatch.setattr(settings, "OIDC_CLIENT_IDS", ("client-a",))
    built = SharedAudience()
    assert (built.issuer, built.audience, built.client_ids) == (
        ISSUER,
        "api://default",
        frozenset({"client-a"}),
    )
    assert "client-a" in built.startup_lines()[0][1]


def test_a_listed_oidc_backend_joins_the_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OIDC_ISSUER", ISSUER)
    monkeypatch.setattr(settings, "AUTH_POLICY_FILE", str(tmp_path / "policy.toml"))
    (tmp_path / "policy.toml").write_text(POLICY)
    monkeypatch.setattr(
        settings,
        "AUTH_BACKENDS",
        ["winslow.contrib.oidc.OidcBackend", "winslow.access.backends.TokenBackend"],
    )
    chain = Authenticator.from_settings("0.0.0.0").mode.chain
    assert chain.accepted == "oidc, token"
    oidc = chain.backends[0]
    oidc._key_client = Keys()
    context = chain.context(minted(sub="can"))
    assert context.user == "can" and context.credential is CredentialKind.OIDC


# --- the login ---------------------------------------------------------------------


def test_login_from_stdin_stores_the_token_with_its_kind(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("eyJ.a.b\n"))
    assert OidcBackend.login("wss://etl-host", True) == CredentialEntry(
        token="eyJ.a.b", kind=CredentialKind.OIDC
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    with pytest.raises(MisconfigurationError, match="no token given"):
        OidcBackend.login("wss://etl-host", True)


def test_the_browser_login_needs_its_two_settings(monkeypatch):
    monkeypatch.setattr(settings, "OIDC_ISSUER", None)
    with pytest.raises(MisconfigurationError, match="OIDC_ISSUER and OIDC_CLIENT_ID"):
        OidcBackend.login("wss://etl-host", False)


def test_pkce_pairs_a_verifier_with_its_s256_challenge():
    import base64
    import hashlib

    verifier, challenge = pkce()
    digest = hashlib.sha256(verifier.encode()).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert pkce()[0] != verifier


def test_the_browser_flow_end_to_end_through_the_loopback_callback():
    """The two seams replaced: fetch answers the discovery document and the
    token endpoint, open plays the browser and calls the loopback back with
    the code and the state the authorization URL carried."""
    requests = []

    def fetch(url, data=None):
        requests.append((url, data))
        if url.endswith("/.well-known/openid-configuration"):
            return {
                "authorization_endpoint": f"{ISSUER}/v1/authorize",
                "token_endpoint": f"{ISSUER}/v1/token",
            }
        assert url == f"{ISSUER}/v1/token"
        assert data["grant_type"] == "authorization_code"
        assert data["code"] == "the-code"
        assert data["code_verifier"]
        return {"access_token": "eyJ.x.y", "refresh_token": "r", "expires_in": 60}

    def open(url):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        assert url.startswith(f"{ISSUER}/v1/authorize?")
        assert query["client_id"] == ["native-app"]
        assert query["code_challenge_method"] == ["S256"]
        assert query["scope"] == ["openid groups"]
        callback = query["redirect_uri"][0]
        back = f"{callback}?code=the-code&state={query['state'][0]}"
        with urllib.request.urlopen(back, timeout=10) as response:
            assert b"close this tab" in response.read()
        return True

    import urllib.request

    login = BrowserLogin(
        ISSUER, "native-app", ("openid", "groups"), fetch=fetch, open=open
    )
    entry = login.run(timeout=10)
    assert entry.token == "eyJ.x.y"
    assert entry.kind is CredentialKind.OIDC
    assert entry.refresh_token == "r"
    assert entry.client == {"client_id": "native-app", "issuer": ISSUER}
    assert entry.expires_at >= int(time.time()) + 55
    assert [url for url, _ in requests] == [
        f"{ISSUER}/.well-known/openid-configuration",
        f"{ISSUER}/v1/token",
    ]


def test_a_callback_with_the_wrong_state_or_an_error_refuses():
    def fetch(url, data=None):
        return {
            "authorization_endpoint": f"{ISSUER}/v1/authorize",
            "token_endpoint": f"{ISSUER}/v1/token",
        }

    import urllib.request

    def open_with_wrong_state(url):
        callback = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)[
            "redirect_uri"
        ][0]
        urllib.request.urlopen(f"{callback}?code=c&state=nope", timeout=10).read()

    def open_with_error(url):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        urllib.request.urlopen(
            f"{query['redirect_uri'][0]}?error=access_denied&state={query['state'][0]}",
            timeout=10,
        ).read()

    login = BrowserLogin(
        ISSUER, "app", ("openid",), fetch=fetch, open=open_with_wrong_state
    )
    with pytest.raises(MisconfigurationError, match="state of the callback"):
        login.run(timeout=10)
    login = BrowserLogin(ISSUER, "app", ("openid",), fetch=fetch, open=open_with_error)
    with pytest.raises(MisconfigurationError, match="access_denied"):
        login.run(timeout=10)


def test_a_backend_owns_the_entries_of_its_own_issuer(monkeypatch):
    class Okta(OidcBackend):
        issuer = "https://okta.example/"

    monkeypatch.setattr(settings, "OIDC_ISSUER", ISSUER)
    of_issuer = CredentialEntry(
        token="t", kind=CredentialKind.OIDC, client={"issuer": ISSUER}
    )
    of_okta = CredentialEntry(
        token="t", kind=CredentialKind.OIDC, client={"issuer": "https://okta.example"}
    )
    hand_edited = CredentialEntry(
        token="t", kind=CredentialKind.OIDC, refresh_token="r"
    )
    assert OidcBackend.owns(of_issuer) and not Okta.owns(of_issuer)
    assert Okta.owns(of_okta) and not OidcBackend.owns(of_okta)
    assert not OidcBackend.owns(hand_edited)
    assert not OidcBackend.owns(
        CredentialEntry(token="wsl_x", kind=CredentialKind.TOKEN)
    )


def test_the_callback_answers_its_own_path_once():
    """A favicon request before or after the callback leaves the login intact."""
    import urllib.error
    import urllib.request
    from http.server import HTTPServer

    from winslow.contrib.oidc import _Callback

    callback = _Callback("s1")
    server = HTTPServer(("127.0.0.1", 0), callback.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(f"{base}/favicon.ico", timeout=10)
        assert refused.value.code == 404 and not callback.done.is_set()
        urllib.request.urlopen(f"{base}/callback?code=c&state=s1", timeout=10).read()
        assert callback.done.is_set() and callback.code == "c"
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"{base}/favicon.ico", timeout=10)
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"{base}/callback?code=x&state=nope", timeout=10)
        assert (callback.code, callback.error) == ("c", None)
    finally:
        server.shutdown()
        server.server_close()


# --- the refresh -------------------------------------------------------------------


def stored(refresh_token="r1"):
    return CredentialEntry(
        token="old",
        kind=CredentialKind.OIDC,
        user="can",
        refresh_token=refresh_token,
        expires_at=int(time.time()) - 10,
        client={"client_id": "native-app", "issuer": ISSUER},
    )


def test_refresh_exchanges_the_refresh_token_at_the_issuer_of_the_entry():
    requests = []

    def fetch(url, data=None):
        requests.append((url, data))
        if data is None:
            return {"token_endpoint": f"{ISSUER}/v1/token"}
        return {"access_token": "new", "expires_in": 60}

    fresh = OidcBackend.refresh(stored(), fetch=fetch)
    assert (fresh.token, fresh.user, fresh.refresh_token) == ("new", "can", "r1")
    assert fresh.expires_at >= int(time.time()) + 55
    assert fresh.client == {"client_id": "native-app", "issuer": ISSUER}
    assert requests == [
        (f"{ISSUER}/.well-known/openid-configuration", None),
        (
            f"{ISSUER}/v1/token",
            {
                "grant_type": "refresh_token",
                "client_id": "native-app",
                "refresh_token": "r1",
            },
        ),
    ]


def test_refresh_keeps_a_new_refresh_token_and_needs_one_to_start():
    def fetch(url, data=None):
        if data is None:
            return {"token_endpoint": f"{ISSUER}/v1/token"}
        return {"access_token": "new", "refresh_token": "r2"}

    assert OidcBackend.refresh(stored(), fetch=fetch).refresh_token == "r2"
    assert OidcBackend.refresh(stored(refresh_token=None), fetch=fetch) is None


def test_a_refused_refresh_names_the_login_command():
    def fetch(url, data=None):
        raise OSError("HTTP Error 400: invalid_grant")

    with pytest.raises(MisconfigurationError, match="invalid_grant.*--kind oidc"):
        OidcBackend.refresh(stored(), fetch=fetch)


def test_a_login_and_a_refresh_follow_the_issuer_conventions_of_a_subclass(monkeypatch):
    """Google shaped: the id token is the credential, the refresh token comes
    from a query parameter, and the token requests carry the client secret."""
    requests = []

    def fetch(url, data=None):
        requests.append((url, data))
        if data is None:
            return {
                "authorization_endpoint": f"{ISSUER}/auth",
                "token_endpoint": f"{ISSUER}/token",
            }
        return {
            "access_token": "ya29.opaque",
            "id_token": "eyJ.id.token",
            "refresh_token": "r",
        }

    login = BrowserLogin(
        ISSUER,
        "app",
        ("openid", "email"),
        token_field="id_token",
        query={"access_type": "offline"},
        client_secret="s3",
        fetch=fetch,
        open=lambda url: None,
    )
    assert "access_type=offline" in login.authorization_url(
        f"{ISSUER}/auth", "r", "st", "ch"
    )
    entry = login.exchange(f"{ISSUER}/token", "code", "v", "r")
    assert (entry.token, entry.refresh_token) == ("eyJ.id.token", "r")
    assert requests[-1][1]["client_secret"] == "s3"

    class Google(OidcBackend):
        token_field = "id_token"

    monkeypatch.setattr(settings, "OIDC_CLIENT_SECRET", "s3")
    fresh = Google.refresh(stored(), fetch=fetch)
    assert fresh.token == "eyJ.id.token"
    assert requests[-1][1]["client_secret"] == "s3"
