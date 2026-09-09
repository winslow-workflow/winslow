"""The serve credentials (ROADMAP section 8): a bearer token for machine
clients, a signed ticket for browsers. The ticket is user:expiry:signature,
HMAC-SHA256 under the shared secret, minted by the front application; this
module owns mint and verify, so both sides and the tests share one rule."""

import hashlib
import hmac
import os
import time
from dataclasses import dataclass


def _sign(secret, user, expiry):
    return hmac.new(
        secret.encode(), f"{user}:{expiry}".encode(), hashlib.sha256
    ).hexdigest()


def mint_ticket(secret, user, ttl=60.0):
    """A ticket the front application hands to an authenticated browser."""
    expiry = time.time() + ttl
    return f"{user}:{expiry}:{_sign(secret, user, expiry)}"


def verify_ticket(secret, ticket):
    """(user, None) for a valid ticket, (None, reason) otherwise. The reason
    is wire-safe: it reaches the client in the refusal."""
    try:
        user, expiry, signature = ticket.split(":")
    except (ValueError, AttributeError):
        return None, "malformed ticket"
    if not hmac.compare_digest(signature, _sign(secret, user, expiry)):
        return None, "bad ticket signature"
    if float(expiry) < time.time():
        return None, "ticket expired - fetch a fresh one and reconnect"
    return user, None


@dataclass(frozen=True)
class Credentials:
    """The credential policy of one serve process. require_credential is
    False only for a loopback bind."""

    token: str | None = None
    ticket_secret: str | None = None
    allowed_origins: tuple = ()
    require_credential: bool = True

    @classmethod
    def from_env(cls, host):
        loopback = host in ("127.0.0.1", "::1", "localhost")
        origins = os.environ.get("WINSLOW_ORIGINS", "")
        return cls(
            token=os.environ.get("WINSLOW_TOKEN"),
            ticket_secret=os.environ.get("WINSLOW_TICKET_SECRET"),
            allowed_origins=tuple(o for o in origins.split(",") if o),
            require_credential=not loopback,
        )

    def verify_hello(self, hello, origin):
        """(user, None) for an accepted hello, (None, reason) for a refusal."""
        if not self.require_credential:
            return "local", None
        if origin is not None and origin not in self.allowed_origins:
            return None, f"origin {origin!r} is not allowed on this server"
        if ticket := hello.get("ticket"):
            if not self.ticket_secret:
                return None, "this server accepts no tickets - use a bearer token"
            return verify_ticket(self.ticket_secret, ticket)
        if token := hello.get("token"):
            if self.token and hmac.compare_digest(token, self.token):
                return "token-client", None
            return None, "bad bearer token"
        return None, "the hello carries no credential - send a ticket or a token"
