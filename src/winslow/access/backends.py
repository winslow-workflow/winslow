"""The auth backends: one class per kind of credential a server accepts. The
AUTH_BACKENDS setting lists them as dotted paths, in chain order. The module
imports no serve extra, so `winslow auth login` runs on a client install."""

import getpass
import logging
import sys
import time
from dataclasses import dataclass

from winslow.access.credentials import CredentialEntry, credential_for
from winslow.access.file import FilePolicyStore, FileTokenStore
from winslow.access.permissions import CredentialKind, Identity, PermissionContext
from winslow.access.policy import policy_scopes, policy_warnings
from winslow.access.tickets import checked_ticket_secret, verify_ticket
from winslow.access.tokens import TOKEN_PREFIX, expired, token_hash
from winslow.conf import import_string, settings
from winslow.exceptions import CredentialRefused, MisconfigurationError

log = logging.getLogger(__name__)


class AuthBackend:
    """The base of one kind of credential. name is the word of the startup
    line, the refusal text and `auth login --kind`. kind is the CredentialKind
    the records carry. The policy file is the permission rule by default."""

    name = None
    kind = None
    # The server an agent logs in at, named by the MCP resource metadata.
    # None when the backend issues nothing itself: a token is minted, a
    # ticket is handed over (see winslow.serve.mcp.McpEndpoint).
    authorization_server = None
    # The scopes a login at that server asks for. The MCP resource metadata
    # lists them, so an agent requests the same ones.
    scopes = ()

    def __init__(self, policy=None):
        self.policy = policy or FilePolicyStore()

    def accepts(self, token):
        """Whether the token has the shape of this kind."""
        raise NotImplementedError

    def identify(self, token):
        """The Identity of a valid token. A refusal raises CredentialRefused."""
        raise NotImplementedError

    def scopes_for(self, identity):
        """The expanded scope set of the identity: the scopes it carries, else
        the policy file with the groups it asserts (see policy_scopes)."""
        if identity.scopes is not None:
            return identity.scopes
        return policy_scopes(
            self.policy.load(), identity.user, idp_groups=identity.groups
        )

    def startup_lines(self):
        """(level, line) pairs for the startup log."""
        return ()

    @classmethod
    def login(cls, url, from_stdin):
        """The CredentialEntry to store for the url, with kind set (see
        winslow.access.cli)."""
        raise NotImplementedError

    @classmethod
    def owns(cls, entry):
        """Whether a stored entry is one this backend obtained, so its refresh
        applies (see refresh_expired). The kind decides by default."""
        return entry.kind == cls.kind

    @classmethod
    def refresh(cls, entry):
        """A fresh CredentialEntry for an expired one, or None when the kind
        has no refresh (see refresh_expired)."""
        return None


def _prompted(kind, url, from_stdin):
    """The entry of a credential a person pastes: one line of stdin, so a
    script keeps it out of the process list, else a prompt without echo."""
    if from_stdin:
        token = sys.stdin.readline().strip()
    else:
        token = getpass.getpass(f"{kind.capitalize()} for {url}: ").strip()
    if not token:
        raise MisconfigurationError(f"no {kind} given - nothing stored.")
    return CredentialEntry(token=token, kind=kind)


class TokenBackend(AuthBackend):
    """A per-user token from the token store, with the scopes of its user
    from the policy file at the time of the hello (see winslow.access)."""

    kind = CredentialKind.TOKEN
    name = "token"

    def __init__(self, tokens=None, policy=None):
        super().__init__(policy)
        self.tokens = tokens or FileTokenStore()

    def accepts(self, token):
        return token.startswith(TOKEN_PREFIX)

    def identify(self, token):
        """The Identity of a token the store knows, with the expiry of the
        token, None when it has none."""
        record = self.tokens.get(token_hash(token))
        if record is None:
            raise CredentialRefused("unknown token - it was revoked or never issued")
        if expired(record):
            raise CredentialRefused("token expired - ask for a new one")
        return Identity(record.user, self.kind, record.token_id, record.expires_at)

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

    @classmethod
    def login(cls, url, from_stdin):
        return _prompted(cls.kind, url, from_stdin)


class TicketBackend(AuthBackend):
    """A ticket by its JWT shape: three segments where a wsl_ token has one
    (see winslow.access.tickets.verify_ticket). The ticket carries its scopes.
    The audience is the public URL of the host."""

    kind = CredentialKind.TICKET
    name = "ticket"

    def __init__(self, secret=None, audience=None):
        super().__init__()
        secret = secret or settings.AUTH_TICKET_SECRET
        audience = audience or settings.SERVE_PUBLIC_URL
        if secret is None or audience is None:
            raise MisconfigurationError(
                "the ticket backend is listed in AUTH_BACKENDS - set "
                "AUTH_TICKET_SECRET and SERVE_PUBLIC_URL in the settings module."
            )
        self.secret = checked_ticket_secret(secret, audience)
        self.audience = audience

    def accepts(self, token):
        return token.count(".") == 2

    def identify(self, token):
        return verify_ticket(self.secret, token, self.audience)

    @classmethod
    def login(cls, url, from_stdin):
        return _prompted(cls.kind, url, from_stdin)


@dataclass(frozen=True)
class BackendChain:
    """The backends a server accepts, in order. The first backend whose shape
    matches a token decides (see AuthBackend.accepts). A backend that raises
    anything but CredentialRefused is broken: the chain logs it once and
    refuses the token, so the connection gets a refusal and no crash."""

    backends: tuple

    @property
    def accepted(self):
        """The accepted credential kinds by name, for a log line or a refusal."""
        return ", ".join(backend.name for backend in self.backends) or "no credential"

    def select(self, token):
        """The first backend whose shape matches the token, or None."""
        return next((b for b in self.backends if b.accepts(token)), None)

    def context(self, token):
        """The PermissionContext of the token: who presented it, then what
        they may do (see AuthBackend). A refusal raises CredentialRefused."""
        backend = self.select(token)
        if backend is None:
            raise CredentialRefused(
                f"bad bearer token - this server accepts {self.accepted}"
            )
        try:
            identity = backend.identify(token)
            return PermissionContext.of(identity, backend.scopes_for(identity))
        except CredentialRefused:
            raise
        except Exception:
            log.exception("the %s backend failed on a credential", backend.name)
            raise CredentialRefused(
                f"the {backend.name} backend failed - the server log has the reason"
            ) from None


def backend_classes():
    """The backend classes AUTH_BACKENDS lists, in order."""
    return tuple(import_string(path) for path in settings.AUTH_BACKENDS)


def backends():
    """The backends AUTH_BACKENDS lists, built in order with their defaults."""
    return tuple(cls() for cls in backend_classes())


def refresh_expired(store, url):
    """Replace an expired entry of the URL with the refresh of its backend,
    before connect sends it. An entry without an expiry, or of a kind with no
    refresh, stays as it is."""
    entry = store.get(url)
    if entry is None or entry.expires_at is None or entry.expires_at > time.time():
        return
    owner = next((cls for cls in backend_classes() if cls.owns(entry)), None)
    fresh = owner.refresh(entry) if owner is not None else None
    if fresh is not None:
        store.set(url, fresh)


class StoredCredential:
    """The credential connect sends: the entry of the URL in the store,
    renewed through its backend when it has expired (see refresh_expired).
    The wire asks on every open, so a reconnect after an expiry carries a
    fresh one."""

    def __init__(self, store, url):
        self.store = store
        self.url = url

    def token(self):
        refresh_expired(self.store, self.url)
        return credential_for(self.url, self.store)


class GivenCredential:
    """A credential handed in as one string: the token of a job, the ticket
    of a test. None is a connection request without a credential."""

    def __init__(self, token=None):
        self._token = token

    def token(self):
        return self._token


def token_backend():
    """The listed TokenBackend the `winslow auth` commands run over."""
    for cls in backend_classes():
        if issubclass(cls, TokenBackend):
            return cls()
    raise MisconfigurationError(
        "no token backend is listed - add winslow.access.backends.TokenBackend "
        "to AUTH_BACKENDS in the settings module."
    )
