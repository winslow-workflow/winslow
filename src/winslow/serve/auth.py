"""The serve credentials: a bearer token for machine clients and a signed ticket
for browsers and scripts. The front application mints a ticket as a JWT under
HS256 with the shared secret: the user, the scopes, the audience and the expiry.
This module owns mint and verify, so both sides share one rule."""

import logging
import time
import uuid
from dataclasses import MISSING, dataclass, fields
from typing import Annotated

import jwt
from pydantic import AfterValidator, BeforeValidator, ConfigDict, Field, Strict

from winslow import settings
from winslow.access import (
    POLICY_BACKENDS,
    TOKEN_BACKENDS,
    TOKEN_PREFIX,
    Name,
    expired,
    policy_warnings,
    scopes_for,
    token_hash,
)
from winslow.constants import LOOPBACK_HOSTS
from winslow.exceptions import CredentialRefused, MisconfigurationError
from winslow.permissions import (
    ANONYMOUS,
    LOCAL,
    CredentialKind,
    PermissionContext,
    expand,
)
from winslow.protocol.codec import CODEC, ValidationError, report


# RFC 8725: the verifier pins one algorithm, so it refuses `alg: none` and an
# asymmetric token before the signature check.
TICKET_ALGORITHM = "HS256"
# The front application and the winslow host keep separate clocks: a ticket
# minted up to this many seconds in the future still verifies (RFC 7519 4.1.4).
TICKET_LEEWAY = 60
# RFC 7518 section 3.2: an HMAC key is at least as long as the hash output.
TICKET_SECRET_BYTES = 32
# Nothing revokes a ticket, so its lifetime is the only bound on a leaked one.
TICKET_MAX_TTL = 3600
TICKET_TTL = 900


def checked_ticket_secret(secret, public_url):
    """The secret when it can sign a ticket: 32 bytes or more, with the
    audience set. None stays None: the process accepts no tickets."""
    if secret is None:
        return None
    if public_url is None:
        raise MisconfigurationError(
            "WINSLOW_TICKET_SECRET is set without WINSLOW_PUBLIC_URL - set the URL "
            "clients reach this server on, it is the audience of every ticket."
        )
    if len(secret.encode()) < TICKET_SECRET_BYTES:
        raise MisconfigurationError(
            f"WINSLOW_TICKET_SECRET is shorter than {TICKET_SECRET_BYTES} bytes - "
            f"use the output of `openssl rand -base64 32`."
        )
    return secret


def _scope_string(scope):
    """RFC 9068 spells the scopes as one space separated string, Okta as a
    list. Anything else is refused."""
    if isinstance(scope, str):
        return scope
    if isinstance(scope, (list, tuple)) and all(isinstance(s, str) for s in scope):
        return " ".join(scope)
    raise ValueError(
        "scope is a space separated string, for example 'etl:run_task reports:view_task'"
    )


def _checked_scope(scope):
    """The canonical spelling of a scope claim that grants at least one scope."""
    spellings = scope.split()
    if not spellings:
        raise ValueError("scope is empty - a ticket grants at least one scope")
    expand(spellings)
    return " ".join(spellings)


Text = Annotated[str, Strict(), Field(min_length=1)]
ScopeClaim = Annotated[
    str, BeforeValidator(_scope_string), AfterValidator(_checked_scope)
]
# RFC 7519 section 2: seconds since the epoch. Strict, so "123" is refused.
NumericDate = Annotated[int, Strict(), Field(ge=0)]


@dataclass(frozen=True)
class TicketClaims:
    """The claims of a ticket: the registered names of RFC 7519 section 4.1
    and scope (RFC 9068). A claim outside this list refuses the ticket."""

    sub: Name
    scope: ScopeClaim
    aud: Text
    iat: NumericDate
    exp: NumericDate
    # The id of the ticket: the credential_id of its log records (spec 2.7).
    jti: Text
    iss: Text = None
    nbf: NumericDate = None
    __pydantic_config__ = ConfigDict(extra="forbid")

    def __post_init__(self):
        if self.exp <= self.iat:
            raise ValueError("exp is not after iat")
        checked_ttl(self.exp - self.iat)
        if self.nbf is not None and self.nbf > self.exp:
            raise ValueError("nbf is after exp")

    @property
    def scopes(self):
        return expand(self.scope.split())


# The claims the verifier requires before the signature check: every field of
# the schema without a default.
TICKET_CLAIMS = tuple(f.name for f in fields(TicketClaims) if f.default is MISSING)


def ticket_claims(payload):
    """The TicketClaims of a payload. A payload the schema refuses raises
    ValueError with the claim and the reason."""
    try:
        return CODEC.decode(TicketClaims, payload)
    except ValidationError as exc:
        raise ValueError(report(exc)) from None


def checked_ttl(ttl):
    """The lifetime when a ticket may live that long (see TICKET_MAX_TTL)."""
    if ttl > TICKET_MAX_TTL:
        raise ValueError(
            f"ticket lives longer than {TICKET_MAX_TTL}s - mint a shorter one, a "
            f"per-user token is the credential with a long life"
        )
    return ttl


def mint_ticket(secret, user, scopes, audience, ttl=TICKET_TTL):
    """A ticket the front application hands to an authenticated browser or a
    script: a JWT that names the user, the scopes, the audience (the
    WINSLOW_PUBLIC_URL of the host) and the expiry. The page fetches a fresh
    one at the expiry, so ttl is how long a logout takes to reach a page. The
    claims and the secret meet the rules of the verifier (see TicketClaims)."""
    now = int(time.time())
    claims = ticket_claims(
        {
            "sub": user,
            "scope": scopes,
            "aud": audience,
            "iat": now,
            "exp": now + ttl,
            "jti": uuid.uuid4().hex,
        }
    )
    payload = {k: v for k, v in CODEC.dump(claims).items() if v is not None}
    secret = checked_ticket_secret(secret, audience)
    return jwt.encode(payload, secret, algorithm=TICKET_ALGORITHM)


def verify_ticket(secret, ticket, audience):
    """The PermissionContext of a valid ticket: the user and the scopes from
    the claims, the expiry from exp. A ticket the rules refuse raises
    CredentialRefused with the reason the client reads."""
    try:
        payload = jwt.decode(
            ticket,
            secret,
            algorithms=[TICKET_ALGORITHM],
            audience=audience,
            leeway=TICKET_LEEWAY,
            options={"require": list(TICKET_CLAIMS)},
        )
        claims = ticket_claims(payload)
    except jwt.ExpiredSignatureError:
        raise CredentialRefused("ticket expired - fetch a fresh one and reconnect")
    except jwt.InvalidAudienceError:
        raise CredentialRefused(f"ticket for another server - this one is {audience}")
    except (jwt.InvalidTokenError, ValueError) as exc:
        raise CredentialRefused(f"bad ticket - {exc}") from None
    return PermissionContext(
        user=claims.sub,
        scopes=claims.scopes,
        credential=CredentialKind.TICKET,
        expires_at=float(claims.exp),
    )


class TokenCredential:
    """A per-user token from the token store, with the scopes of its user
    from the policy store at the time of the hello (see winslow.access)."""

    kind = CredentialKind.TOKEN
    name = "per-user token"

    def __init__(self, tokens, policy):
        self.tokens = tokens
        self.policy = policy

    def accepts(self, token):
        return token.startswith(TOKEN_PREFIX)

    def verify(self, token):
        """The PermissionContext of a token the store knows, with the expiry
        of the token, None when it has none."""
        record = self.tokens.get(token_hash(token))
        if record is None:
            raise CredentialRefused("unknown token - it was revoked or never issued")
        if expired(record):
            raise CredentialRefused("token expired - ask for a new one")
        return PermissionContext(
            user=record.user,
            scopes=scopes_for(self.policy.load(), record.user),
            credential=self.kind,
            expires_at=record.expires_at,
        )

    def startup_lines(self):
        """The store names, and one WARNING per policy entry that grants
        everything or nothing."""
        stores = (
            f"auth: policy {type(self.policy).__name__}, tokens "
            f"{type(self.tokens).__name__}"
        )
        warnings = (
            (logging.WARNING, f"auth: {sentence}")
            for sentence in policy_warnings(self.policy.load())
        )
        return ((logging.INFO, stores), *warnings)


class TicketCredential:
    """A ticket by its JWT shape: three segments where a wsl_ token has one
    (see verify_ticket). The audience is the public URL of the host."""

    kind = CredentialKind.TICKET
    name = "ticket"

    def __init__(self, secret, audience):
        self.secret = checked_ticket_secret(secret, audience)
        self.audience = audience

    def accepts(self, token):
        return token.count(".") == 2

    def verify(self, token):
        return verify_ticket(self.secret, token, self.audience)

    def startup_lines(self):
        return ()


class LoopbackMode:
    """A loopback bind: every hello and every MCP request passes as local.
    Every process on the machine reaches a loopback server."""

    checks_bearer = False

    def __init__(self, credentials):
        self.credentials = credentials

    def hello_context(self, hello):
        return LOCAL

    def mcp_context(self, access_token):
        return LOCAL

    def startup_lines(self, bind):
        return ((logging.INFO, f"auth: loopback bind {bind} - every hello passes"),)


class OpenMode:
    """The development switch (see WINSLOW_AUTH_OPEN): every hello and every
    MCP request passes with every permission. A credential the chain resolves
    still names its user, so the records name the person when one is known."""

    checks_bearer = False

    def __init__(self, credentials):
        self.credentials = credentials

    def hello_context(self, hello):
        try:
            resolved = self.credentials.verify_bearer(hello.token or "")
        except CredentialRefused:
            return ANONYMOUS
        return PermissionContext.full(
            resolved.user, resolved.credential, resolved.expires_at
        )

    def mcp_context(self, access_token):
        return ANONYMOUS

    def startup_lines(self, bind):
        return (
            (
                logging.WARNING,
                f"auth: open mode - every client on {bind} holds every "
                f"permission. Use it on a laptop only.",
            ),
        )


class CredentialMode:
    """Every other bind: the chain decides on the websocket hello and on the
    MCP bearer header alike (see Credentials.verify_bearer)."""

    checks_bearer = True

    def __init__(self, credentials):
        self.credentials = credentials

    def hello_context(self, hello):
        if not hello.token:
            raise CredentialRefused("the hello carries no credential - send a token")
        return self.credentials.verify_bearer(hello.token)

    def mcp_context(self, access_token):
        """The context the verifier put on the token (see BearerTokenVerifier)."""
        return access_token.context

    def startup_lines(self, bind):
        kinds = self.credentials.kinds
        accepted = (
            logging.INFO,
            f"auth: accepted credentials: {self.credentials.accepted}",
        )
        return (accepted, *(line for kind in kinds for line in kind.startup_lines()))


@dataclass(frozen=True)
class Credentials:
    """The credential policy of one serve process. chain holds the credential
    kinds it accepts, in order (see TokenCredential, TicketCredential).
    require_credential is False for a loopback bind alone. open_mode is the
    development switch (see OpenMode). max_connection is the socket bound in
    seconds (see verify_hello)."""

    chain: tuple = None
    require_credential: bool = True
    open_mode: bool = False
    max_connection: float = 3600.0

    @classmethod
    def from_env(cls, host):
        """The policy of one bind: the two stores, and tickets when a secret
        is set."""
        tickets = (
            (TicketCredential(settings.SERVE_TICKET_SECRET, settings.PUBLIC_URL),)
            if settings.SERVE_TICKET_SECRET
            else ()
        )
        return cls(
            chain=(
                TokenCredential(TOKEN_BACKENDS.create(), POLICY_BACKENDS.create()),
                *tickets,
            ),
            require_credential=host not in LOOPBACK_HOSTS,
            open_mode=settings.AUTH_OPEN,
            max_connection=settings.AUTH_MAX_CONNECTION,
        )

    @property
    def kinds(self):
        return self.chain or ()

    @property
    def accepted(self):
        """The accepted credential kinds by name, for a log line or a refusal."""
        return ", ".join(kind.name for kind in self.kinds) or "no credential"

    @property
    def mode(self):
        """The mode of the process: open, loopback, or a credential required."""
        if self.open_mode:
            return OpenMode(self)
        if not self.require_credential:
            return LoopbackMode(self)
        return CredentialMode(self)

    def describe(self, bind):
        """(level, line) pairs for the startup log: the mode, the accepted
        credentials and the stores, and a WARNING for what grants everything."""
        return self.mode.startup_lines(bind)

    def verify_bearer(self, token):
        """The PermissionContext of a token a kind of the chain accepts and
        verifies. The first kind whose shape matches decides: a wsl_ token has
        no dot, a JWT has two. A refusal raises CredentialRefused."""
        for kind in self.kinds:
            if kind.accepts(token):
                return kind.verify(token)
        raise CredentialRefused(
            f"bad bearer token - this server accepts {self.accepted}"
        )

    def mcp_context(self, access_token):
        """The PermissionContext of one MCP request (see the modes)."""
        return self.mode.mcp_context(access_token)

    def verify_hello(self, hello):
        """The PermissionContext of an accepted HelloFrame. A refusal raises
        CredentialRefused. The websocket door caps every context at now plus
        max_connection, so a stale permission lives at most one socket
        lifetime (see PermissionContext.until)."""
        context = self.mode.hello_context(hello)
        return context.until(time.time() + self.max_connection)
