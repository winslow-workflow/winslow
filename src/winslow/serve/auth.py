"""The serve credentials: a bearer token for machine clients and a signed ticket
for browsers. The front application mints a ticket as user:expiry:signature under
HMAC-SHA256 with the shared secret. This module owns mint and verify, so both
sides share one rule."""

import hashlib
import hmac
import time
from dataclasses import dataclass

from winslow import settings


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
    reaches the client in the refusal."""
    try:
        user, expiry, signature = ticket.split(":")
    except (ValueError, AttributeError):
        return None, "malformed ticket"
    if not hmac.compare_digest(
        signature.encode(), _sign(secret, user, expiry).encode()
    ):
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
        origins = settings.SERVE_ORIGINS.split(",")
        return cls(
            token=settings.SERVE_TOKEN,
            ticket_secret=settings.SERVE_TICKET_SECRET,
            allowed_origins=tuple(o for o in origins if o),
            require_credential=not loopback,
        )

    def verify_hello(self, hello, origin):
        """(user, None) for an accepted HelloFrame, (None, reason) for a refusal."""
        # The Origin rule holds on every bind: a page in a local browser reaches
        # a loopback server too.
        if origin is not None and origin not in self.allowed_origins:
            return None, f"origin {origin!r} is not allowed on this server"
        if not self.require_credential:
            return "local", None
        if ticket := hello.ticket:
            if not self.ticket_secret:
                return None, "this server accepts no tickets - use a bearer token"
            return verify_ticket(self.ticket_secret, ticket)
        if token := hello.token:
            if self.token and hmac.compare_digest(token.encode(), self.token.encode()):
                return "token-client", None
            return None, "bad bearer token"
        return None, "the hello carries no credential - send a ticket or a token"
