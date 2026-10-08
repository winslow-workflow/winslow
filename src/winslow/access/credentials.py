"""The credential file of one person: the token `winslow auth login` stored
for each server, which `winslow connect` sends when WINSLOW_TOKEN is unset
(see specs/auth-step-5-spec.md). The layout holds the OAuth fields the SDK
adapter of a later step reads, so one file serves both."""

import os
import tomllib
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import urlsplit, urlunsplit

import tomli_w
from pydantic import ConfigDict

from winslow import settings
from winslow.access.file import FileSnapshot, _write_text
from winslow.access.tokens import TOKEN_PREFIX
from winslow.constants import LOOPBACK_HOSTS
from winslow.exceptions import MisconfigurationError
from winslow.permissions import CredentialKind
from winslow.protocol.codec import CODEC, ValidationError, report

LOGIN_SCHEMES = ("ws", "wss")
# The paths connect rewrites to the websocket root (see normalize_url).
ROOT_PATHS = ("", "/", "/ws")


@dataclass(frozen=True)
class CredentialEntry:
    """One stored credential. token is what the hello sends. user is the
    name the server answered with. The rest are the OAuth fields of the
    SDK adapter: an absolute expiry, and the registration stored whole."""

    token: str
    user: str | None = None
    refresh_token: str | None = None
    expires_at: int | None = None
    scope: str | None = None
    client: dict | None = None
    __pydantic_config__ = ConfigDict(extra="forbid")

    @property
    def kind(self):
        if self.refresh_token is not None or self.client is not None:
            return "oauth"
        if self.token.startswith(TOKEN_PREFIX):
            return "per-user token"
        return "ticket"


def credential_key(url):
    """The spelling of a server URL the file keys by: the scheme and the host
    in lower case, the port as given, no path for the websocket root."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    netloc = f"[{host}]" if ":" in host else host
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    path = "" if parts.path in ROOT_PATHS else parts.path
    return urlunsplit((parts.scheme.lower(), netloc, path, "", ""))


def checked_login_url(url, insecure=False):
    """The URL when login may store a credential for it: a websocket scheme,
    and TLS or a loopback host unless insecure is given."""
    parts = urlsplit(url)
    if parts.scheme not in LOGIN_SCHEMES:
        raise MisconfigurationError(
            f"{url!r} is not a server URL - the schemes are {', '.join(LOGIN_SCHEMES)}."
        )
    if parts.scheme == "ws" and parts.hostname not in LOOPBACK_HOSTS and not insecure:
        raise MisconfigurationError(
            f"{url} sends the credential in clear text - use wss://, or pass "
            f"--insecure to store it anyway."
        )
    return url


class CredentialFile(FileSnapshot):
    """The credential file, one table per server key (see credential_key).
    A file that does not parse stops the command: a fresh process has no
    previous value to keep, and the next login must not overwrite a hand
    edit that broke the file."""

    empty = MappingProxyType({})
    noun = "credentials"

    def __init__(self, path=None):
        super().__init__(os.path.expanduser(path or settings.CREDENTIALS_FILE))

    def parse(self, text):
        try:
            tables = tomllib.loads(text)
            return {
                key: CODEC.decode(CredentialEntry, table)
                for key, table in tables.items()
            }
        except (tomllib.TOMLDecodeError, ValidationError) as exc:
            detail = report(exc) if isinstance(exc, ValidationError) else str(exc)
            raise MisconfigurationError(
                f"{self.path}: {detail} - fix the file or remove the broken entry."
            ) from None

    def _write(self, entries):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tables = {
            key: {k: v for k, v in CODEC.dump(entry).items() if v is not None}
            for key, entry in entries.items()
        }
        _write_text(self.path, tomli_w.dumps(tables))
        self._stamp = self.path.stat().st_mtime_ns
        self._value = entries

    def get(self, url):
        return self.load().get(credential_key(url))

    def set(self, url, entry):
        self._write({**self.load(), credential_key(url): entry})

    def delete(self, url):
        key = credential_key(url)
        entries = self.load()
        if key not in entries:
            return False
        self._write({k: v for k, v in entries.items() if k != key})
        return True

    def entries(self):
        return tuple(self.load().items())


def credential_for(url, store, environment):
    """The credential connect sends: environment when it is a non-empty
    string, else the stored token for the URL, else None."""
    if environment:
        return environment
    entry = store.get(url)
    return None if entry is None else entry.token


def remember_user(store, url, hello):
    """Write the user of a hello_ok to the entry of the URL, when the hello
    came from a per-user token or a ticket: a loopback or open-mode server
    names no person."""
    entry = store.get(url)
    named = hello.credential in (CredentialKind.TOKEN, CredentialKind.TICKET)
    if entry is None or not named or entry.user == hello.user:
        return
    store.set(url, CredentialEntry(**{**CODEC.dump(entry), "user": hello.user}))
