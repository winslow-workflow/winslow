"""The ticket: a JWT the front application mints for an authenticated browser
or a script, signed under HS256 with the shared secret. The claims name the
user, the scopes, the audience and the expiry. This module owns mint and
verify, so both sides share one rule."""

import time
import uuid
from dataclasses import MISSING, dataclass, fields
from typing import Annotated

import jwt
from pydantic import AfterValidator, BeforeValidator, ConfigDict, Field, Strict

from winslow.access.permissions import CredentialKind, Identity, expand
from winslow.access.policy import Name
from winslow.exceptions import CredentialRefused, MisconfigurationError
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
            "AUTH_TICKET_SECRET is set without SERVE_PUBLIC_URL - set the URL clients "
            "reach this server on, it is the audience of every ticket."
        )
    if len(secret.encode()) < TICKET_SECRET_BYTES:
        raise MisconfigurationError(
            f"AUTH_TICKET_SECRET is shorter than {TICKET_SECRET_BYTES} bytes - "
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
    SERVE_PUBLIC_URL of the host) and the expiry. The page fetches a fresh
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
    """The Identity of a valid ticket: the user and the scopes from the
    claims, the expiry from exp. A ticket the rules refuse raises
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
    return Identity(
        user=claims.sub,
        kind=CredentialKind.TICKET,
        credential_id=claims.jti,
        expires_at=float(claims.exp),
        scopes=claims.scopes,
    )
