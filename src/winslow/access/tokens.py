"""Per-user tokens: the credential a person or a service account presents
when no login exists. The token travels once, at creation; every store holds
its SHA-256 hash alone (see TokenRecord)."""

import base64
import hashlib
import re
import secrets
import time
from dataclasses import dataclass

# The prefix a secret scanner matches on, the way ghp_ and xoxb- are.
TOKEN_PREFIX = "wsl_"

_DURATION = re.compile(r"^(\d+)([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


@dataclass(frozen=True)
class TokenRecord:
    """One token at rest: its hash, its user, and its dates. token_id is
    the first eight hex characters of the hash, the handle a revoke names."""

    token_hash: str
    user: str
    created_at: float
    expires_at: float | None = None

    @property
    def token_id(self):
        return self.token_hash.partition(":")[2][:8]


def new_token():
    """A fresh token: the prefix and 32 random bytes in base64url."""
    raw = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    return f"{TOKEN_PREFIX}{raw}"


def token_hash(token):
    return f"sha256:{hashlib.sha256(token.encode()).hexdigest()}"


def expired(record, now=None):
    return record.expires_at is not None and record.expires_at <= (
        time.time() if now is None else now
    )


def parse_duration(text):
    """The seconds of a duration such as 90d, 12h, 30m or 45s."""
    match = _DURATION.match(text.strip())
    if match is None:
        raise ValueError(
            f"{text!r} is not a duration - write a number and a unit, for example 90d, 12h or 30m."
        )
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]
