"""The OpenID Connect backend: an access token of an issuer, verified against
the key set of the issuer, with the scopes of its user from the policy file
and the groups the token asserts. The backend is generic: a deployment lists
it in AUTH_BACKENDS and sets the OIDC_* settings, or subclasses it with its
own defaults and its own scopes_for (see docs/serve.md). pyjwt comes with
the serve, the mcp and the connect extras."""

import base64
import hashlib
import json
import logging
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Annotated

import jwt
from pydantic import AfterValidator, BeforeValidator, Field

from winslow.access.backends import AuthBackend, Identity
from winslow.access.credentials import CredentialEntry
from winslow.access.permissions import CredentialKind
from winslow.access.tickets import NumericDate, Text
from winslow.conf import settings
from winslow.exceptions import CredentialRefused, MisconfigurationError
from winslow.protocol.codec import CODEC, ValidationError, report
from winslow.util import to_tuple

ALGORITHMS = ("RS256", "ES256")
# The issuer and the server may disagree on the clock by this much.
LEEWAY = 60
# The claim that names the client a token was issued to, by presence, so an
# empty first claim cannot hand the decision to the next.
CLIENT_CLAIMS = ("cid", "client_id", "azp")
DISCOVERY_PATH = "/.well-known/openid-configuration"


def _fetch_json(url, data=None):
    """One HTTP request as JSON: a GET, or a form POST when data is given."""
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    with urllib.request.urlopen(url, data=body, timeout=30) as response:
        return json.loads(response.read())


def _client_of(claims):
    for name in CLIENT_CLAIMS:
        if name in claims:
            return claims[name]
    return None


def _listed_client(client, info):
    """The client when the allow list of the context names it, or when the
    list is empty."""
    clients = info.context["client_ids"]
    if clients and client not in clients:
        raise ValueError(
            f"token of client {client!r} - this server accepts {', '.join(sorted(clients))}"
        )
    return client


def _at_allowed_domain(user, info):
    """The user when the allowed domains of the context hold its domain, or
    when no domain is set."""
    domains = info.context["allowed_domains"]
    local, _, domain = user.rpartition("@")
    if domains and (not local or domain.lower() not in domains):
        raise ValueError(f"{user!r} is not a login at {', '.join(sorted(domains))}")
    return user


User = Annotated[Text, AfterValidator(_at_allowed_domain)]
Client = Annotated[str | None, AfterValidator(_listed_client)]
# An issuer sends one group as a bare string: to_tuple keeps it whole.
Groups = Annotated[tuple[Text, ...], BeforeValidator(to_tuple)]


@dataclass(frozen=True)
class OidcClaims:
    """The claims of a verified token, under the names winslow reads: the
    backend maps the user claim and the groups claim of its issuer onto user
    and groups (see OidcBackend.named). The client allow list and the allowed
    domains reach the validators as the context of the decode."""

    user: User
    exp: NumericDate
    # Validated on its default too: a token with no client claim is refused
    # by an allow list.
    client: Client = Field(default=None, validate_default=True)
    groups: Groups = None
    jti: Text = None


class OidcBackend(AuthBackend):
    """An access token of one issuer. The signature verifies against the key
    set of the issuer, iss and aud must match, exp and iat are required, and
    the client allow-list applies when set. The user is the sub claim, at one
    of the allowed domains when any are set. The groups claim reaches
    scopes_for as the groups the token asserts."""

    kind = CredentialKind.OIDC
    name = "oidc"
    # The conventions of the issuer. A subclass sets the ones its issuer
    # differs on: the issuer itself when two are listed, the audience it
    # stamps when that is not the SERVE_PUBLIC_URL, the key set URL when the
    # discovery document names none, the claims, the scopes of the login.
    issuer = None
    audience = None
    jwks_url = None
    user_claim = "sub"
    groups_claim = "groups"
    # offline_access asks the issuer for a refresh token (OpenID Connect Core
    # section 11), so a login outlives the first access token (see refresh).
    scopes = ("openid", "offline_access")
    # The field of the token response the client stores and sends. An issuer
    # whose access token is opaque, Google, verifies by its id token instead.
    token_field = "access_token"
    # Extra query parameters of the authorization URL, for an issuer that
    # asks for its refresh token outside the scopes (access_type=offline).
    login_query = None

    def __init__(
        self,
        issuer=None,
        audience=None,
        client_ids=None,
        allowed_domains=None,
        policy=None,
        key_client=None,
    ):
        super().__init__(policy)
        issuer = issuer or type(self).issuer or settings.OIDC_ISSUER
        audience = audience or type(self).audience or settings.SERVE_PUBLIC_URL
        client_ids = settings.OIDC_CLIENT_IDS if client_ids is None else client_ids
        allowed_domains = (
            settings.OIDC_ALLOWED_DOMAINS
            if allowed_domains is None
            else allowed_domains
        )
        if issuer is None:
            raise MisconfigurationError(
                "the OIDC backend is listed in AUTH_BACKENDS - set OIDC_ISSUER "
                "in the settings module."
            )
        if audience is None:
            raise MisconfigurationError(
                "the OIDC backend needs an audience - set SERVE_PUBLIC_URL in the settings module."
            )
        if audience != settings.SERVE_PUBLIC_URL and not client_ids:
            raise MisconfigurationError(
                f"the audience {audience!r} differs from SERVE_PUBLIC_URL, so every app of the "
                f"issuer shares it - set OIDC_CLIENT_IDS to the clients of this server, "
                f"so a token of another app is refused."
            )
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.client_ids = frozenset(client_ids)
        self.allowed_domains = frozenset(d.lower() for d in allowed_domains)
        # The context of the claims decode (see OidcClaims).
        self.rules = {
            "client_ids": self.client_ids,
            "allowed_domains": self.allowed_domains,
        }
        # The key client fetches on the first verify, so the construction needs
        # no network (see keys).
        self._key_client = key_client

    @property
    def keys(self):
        """The key client of the issuer. A key id outside the cached set is
        refused until the set expires, so a bad token costs no fetch."""
        if self._key_client is None:
            url = self.jwks_url or _fetch_json(self.issuer + DISCOVERY_PATH)["jwks_uri"]
            self._key_client = jwt.PyJWKClient(url, cache_keys=True)
        return self._key_client

    @property
    def authorization_server(self):
        return self.issuer

    def accepts(self, token):
        """A JWT whose unverified iss names this issuer."""
        if token.count(".") != 2:
            return False
        try:
            claims = jwt.decode(token, options={"verify_signature": False})
        except jwt.InvalidTokenError:
            return False
        return claims.get("iss") == self.issuer

    def identify(self, token):
        try:
            key = self.keys.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=list(ALGORITHMS),
                audience=self.audience,
                issuer=self.issuer,
                leeway=LEEWAY,
                options={"require": ["exp", "iat"]},
            )
        except jwt.ExpiredSignatureError:
            raise CredentialRefused("token expired - log in again") from None
        except jwt.InvalidAudienceError:
            raise CredentialRefused(
                f"token for another audience - this server is {self.audience}"
            ) from None
        except jwt.PyJWKClientError as exc:
            raise CredentialRefused(f"token signed by an unknown key - {exc}") from None
        except jwt.InvalidTokenError as exc:
            raise CredentialRefused(f"bad token - {exc}") from None
        try:
            named = CODEC.decode(OidcClaims, self.named(claims), context=self.rules)
        except ValidationError as exc:
            raise CredentialRefused(f"bad token - {report(exc)}") from None
        return Identity(
            user=named.user,
            kind=self.kind,
            credential_id=named.jti,
            expires_at=float(named.exp),
            groups=named.groups,
        )

    def named(self, claims):
        """The claims present in the payload, under the names of OidcClaims."""
        picked = {
            "user": claims.get(self.user_claim),
            "client": _client_of(claims),
            "groups": claims.get(self.groups_claim),
            "jti": claims.get("jti"),
            "exp": claims.get("exp"),
        }
        return {name: value for name, value in picked.items() if value is not None}

    def startup_lines(self):
        clients = ", ".join(sorted(self.client_ids)) or "any client"
        return (
            (
                logging.INFO,
                f"auth: oidc issuer {self.issuer}, audience {self.audience}, {clients}",
            ),
        )

    @classmethod
    def login(cls, url, from_stdin):
        """The entry of a token read from stdin, or of the browser flow of
        the issuer with PKCE (see BrowserLogin)."""
        if from_stdin:
            token = sys.stdin.readline().strip()
            if not token:
                raise MisconfigurationError("no token given - nothing stored.")
            return CredentialEntry(token=token, kind=cls.kind)
        if settings.OIDC_ISSUER is None or settings.OIDC_CLIENT_ID is None:
            raise MisconfigurationError(
                "an OIDC login needs OIDC_ISSUER and OIDC_CLIENT_ID in the settings "
                "module of this client."
            )
        return BrowserLogin(
            settings.OIDC_ISSUER,
            settings.OIDC_CLIENT_ID,
            cls.scopes,
            token_field=cls.token_field,
            query=cls.login_query,
            client_secret=settings.OIDC_CLIENT_SECRET,
        ).run()

    @classmethod
    def issuer_url(cls):
        """The issuer of this class without a trailing slash: its own, else
        OIDC_ISSUER. None when neither is set."""
        issuer = cls.issuer or settings.OIDC_ISSUER
        return None if issuer is None else issuer.rstrip("/")

    @classmethod
    def owns(cls, entry):
        """An entry of this kind whose registration names this issuer. Two
        listed issuers share the kind, so the issuer decides."""
        if entry.kind != cls.kind or entry.client is None:
            return False
        return entry.client.get("issuer") == cls.issuer_url()

    @classmethod
    def refresh(cls, entry, fetch=_fetch_json):
        """The entry of the refresh token grant at the issuer the entry names
        (RFC 6749 section 6). None without a refresh token."""
        if entry.refresh_token is None:
            return None
        try:
            endpoint = fetch(entry.client["issuer"] + DISCOVERY_PATH)["token_endpoint"]
            tokens = fetch(
                endpoint,
                data=_with_secret(
                    {
                        "grant_type": "refresh_token",
                        "client_id": entry.client["client_id"],
                        "refresh_token": entry.refresh_token,
                    },
                    settings.OIDC_CLIENT_SECRET,
                ),
            )
        except OSError as exc:
            raise MisconfigurationError(
                f"the stored credential expired and the issuer refused the refresh - "
                f"{exc}. Run `winslow auth login <url> --kind {cls.name}` again."
            ) from None
        return entry_of(tokens, entry.client, previous=entry, field=cls.token_field)


def _with_secret(data, client_secret):
    """The token request with the client secret an issuer demands of an
    installed app (Google), when the settings hold one."""
    return data if client_secret is None else {**data, "client_secret": client_secret}


def entry_of(tokens, client, previous=None, field="access_token"):
    """The CredentialEntry of a token response, the token read from `field`.
    A refresh response may omit the refresh token: the previous entry keeps
    it, and its user."""
    expires_in = tokens.get("expires_in")
    return CredentialEntry(
        token=tokens[field],
        kind=CredentialKind.OIDC,
        user=previous.user if previous else None,
        refresh_token=tokens.get("refresh_token")
        or (previous.refresh_token if previous else None),
        expires_at=None if expires_in is None else int(time.time()) + int(expires_in),
        scope=tokens.get("scope"),
        client=client,
    )


def pkce():
    """A PKCE verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class BrowserLogin:
    """The authorization code flow of one issuer, for a person at a terminal:
    a loopback callback, the browser opened on the authorization URL, the code
    exchanged for the tokens. fetch and open are the two seams a test replaces."""

    def __init__(
        self,
        issuer,
        client_id,
        scopes,
        token_field="access_token",
        query=None,
        client_secret=None,
        fetch=_fetch_json,
        open=webbrowser.open,
    ):
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.scopes = tuple(scopes)
        self.token_field = token_field
        self.query = query or {}
        self.client_secret = client_secret
        self.fetch = fetch
        self.open = open

    def endpoints(self):
        """(authorization endpoint, token endpoint) of the discovery document."""
        document = self.fetch(self.issuer + DISCOVERY_PATH)
        return document["authorization_endpoint"], document["token_endpoint"]

    def authorization_url(self, endpoint, redirect_uri, state, challenge):
        query = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            **self.query,
        }
        return f"{endpoint}?{urllib.parse.urlencode(query)}"

    def exchange(self, endpoint, code, verifier, redirect_uri):
        """The CredentialEntry of the token response."""
        tokens = self.fetch(
            endpoint,
            data=_with_secret(
                {
                    "grant_type": "authorization_code",
                    "client_id": self.client_id,
                    "code": code,
                    "code_verifier": verifier,
                    "redirect_uri": redirect_uri,
                },
                self.client_secret,
            ),
        )
        client = {"client_id": self.client_id, "issuer": self.issuer}
        return entry_of(tokens, client, field=self.token_field)

    def run(self, timeout=300):
        """The whole flow, blocking until the callback or the timeout."""
        authorize, token_endpoint = self.endpoints()
        verifier, challenge = pkce()
        state = secrets.token_urlsafe(16)
        callback = _Callback(state)
        server = HTTPServer(("127.0.0.1", 0), callback.handler())
        redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.open(self.authorization_url(authorize, redirect_uri, state, challenge))
            if not callback.done.wait(timeout):
                raise MisconfigurationError(
                    f"no login within {timeout} seconds - run the command again."
                )
        finally:
            server.shutdown()
            server.server_close()
        if callback.error:
            raise MisconfigurationError(
                f"the issuer refused the login - {callback.error}"
            )
        return self.exchange(token_endpoint, callback.code, verifier, redirect_uri)


class _Callback:
    """The one callback of a login: the code and the state the browser brings
    back, or the error of the issuer."""

    def __init__(self, state):
        self.state = state
        self.code = None
        self.error = None
        self.done = threading.Event()

    def handler(self):
        callback = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                # The browser also asks for a favicon, before or after the
                # callback. Only the first request on /callback counts.
                parts = urllib.parse.urlsplit(self.path)
                if parts.path != "/callback" or callback.done.is_set():
                    self.send_error(404)
                    return
                query = urllib.parse.parse_qs(parts.query)
                if query.get("state", [None])[0] != callback.state:
                    callback.error = "the state of the callback does not match"
                elif "error" in query:
                    callback.error = query["error"][0]
                else:
                    callback.code = query.get("code", [None])[0]
                    if callback.code is None:
                        callback.error = "the callback carries no code"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"winslow: you can close this tab.")
                callback.done.set()

            def log_message(self, format, *args):
                return None

        return Handler
