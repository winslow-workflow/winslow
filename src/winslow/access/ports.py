"""The token store port of access control. A store keeps records and
decides nothing: the token rules live in winslow.access.tokens, so a file
store and a database store behave alike (see FileTokenStore). The policy
rule is a method of the backend (see AuthBackend.scopes_for).

The rules a store follows:

- get() runs per hello and per MCP request, on a worker thread, so a slow
  source blocks no socket (see ServeApp._handshake).
- A store answers from a snapshot. A source that fails to read returns the
  previous snapshot and logs the reason (see FileSnapshot).
"""

from typing import Protocol


class TokenStore(Protocol):
    def get(self, token_hash):
        """Return the TokenRecord under the hash, or None."""

    def add(self, record):
        """Store one TokenRecord."""

    def remove(self, token_id):
        """Remove the record with the id. Return True when one was removed."""

    def remove_where(self, user):
        """Remove every record of the user. Return the count."""

    def list(self, user=None):
        """Return the records, of one user when given, oldest first."""
