"""The authenticator of a serve process: the mode of one bind and the chain
of auth backends it runs (see Authenticator). The ticket rules live in
winslow.access.tickets."""

import logging
import time
from dataclasses import dataclass, replace

from winslow.conf import settings
from winslow.access.tickets import mint_ticket, verify_ticket
from winslow.constants import LOCAL_ENV, LOOPBACK_HOSTS
from winslow.exceptions import CredentialRefused
from winslow.access.permissions import ALL, ANONYMOUS, LOCAL

__all__ = ["Authenticator", "mint_ticket", "verify_ticket"]


class LoopbackMode:
    """A loopback bind in the local env: every connection request and every
    MCP request passes as local. Every process on the machine reaches a
    loopback server. A deployment sets ENV, so a loopback bind behind its
    proxy requires a credential."""

    checks_bearer = False

    def get_context(self, token):
        return LOCAL

    def startup_lines(self, bind):
        return (
            (
                logging.INFO,
                f"auth: loopback bind {bind} in the local env - every connection passes",
            ),
        )


class OpenMode:
    """The development switch (see AUTH_OPEN): every connection request and
    every MCP request passes with every permission. A credential the chain
    resolves still names its user, so the records name the person when one
    is known."""

    checks_bearer = False

    def __init__(self, chain):
        self.chain = chain

    def get_context(self, token):
        try:
            resolved = self.chain.context(token or "")
        except CredentialRefused:
            return ANONYMOUS
        return replace(resolved, scopes=ALL)

    def startup_lines(self, bind):
        return (
            (
                logging.WARNING,
                f"auth: open mode - every client on {bind} holds every "
                f"permission. Use it for development only.",
            ),
        )


class CredentialMode:
    """Every other bind: the chain decides on the websocket connection request
    and on the MCP bearer header alike (see BackendChain.context)."""

    checks_bearer = True

    def __init__(self, chain):
        self.chain = chain

    def get_context(self, token):
        if not token:
            raise CredentialRefused("no credential was sent - this server needs one")
        return self.chain.context(token)

    def startup_lines(self, bind):
        accepted = (logging.INFO, f"auth: accepted credentials: {self.chain.accepted}")
        return (
            accepted,
            *(
                line
                for backend in self.chain.backends
                for line in backend.startup_lines()
            ),
        )


@dataclass(frozen=True)
class Authenticator:
    """The authenticator of one serve process: who a connection request or a
    bearer header is from. mode is one of LoopbackMode, OpenMode and
    CredentialMode: get_context answers the PermissionContext of one
    credential, or None, on both doors alike, and holds the chain of auth
    backends where one applies (see winslow.access.backends.BackendChain).
    max_connection is the socket bound in seconds (see verify_hello)."""

    mode: object
    max_connection: int = 3600

    @classmethod
    def from_settings(cls, host):
        """The mode of one bind, picked once: open on AUTH_OPEN, loopback for a
        loopback bind in the local env, else the credential mode over the
        backends AUTH_BACKENDS lists (see winslow.access.backends)."""
        from winslow.access.backends import BackendChain, backends

        chain = BackendChain(backends())
        loopback = host in LOOPBACK_HOSTS and settings.ENV == LOCAL_ENV
        if settings.AUTH_OPEN:
            mode = OpenMode(chain)
        elif loopback:
            mode = LoopbackMode()
        else:
            mode = CredentialMode(chain)
        return cls(mode=mode, max_connection=settings.AUTH_MAX_CONNECTION)

    def describe(self, bind):
        """(level, line) pairs for the startup log: the mode, the accepted
        credentials and the stores, and a WARNING for what grants everything."""
        return self.mode.startup_lines(bind)

    def mcp_context(self, access_token):
        """The PermissionContext of one MCP request: the one the verifier put
        on the bearer (see BearerTokenVerifier), else the one the mode gives a
        request without a credential."""
        if access_token is not None:
            return access_token.context
        return self.mode.get_context(None)

    def verify_hello(self, hello):
        """The PermissionContext of an accepted HelloFrame. A refusal raises
        CredentialRefused. The websocket door caps every context at now plus
        max_connection, so a stale permission lives at most one socket
        lifetime (see PermissionContext.until)."""
        context = self.mode.get_context(hello.token)
        return context.until(time.time() + self.max_connection)
