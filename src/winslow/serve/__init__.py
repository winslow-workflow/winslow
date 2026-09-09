"""The serve process: a websocket endpoint and an optional MCP mount over the
live sessions of a SessionRegistry (see ServeApp). Requires the [serve] extra."""

from .auth import Credentials, mint_ticket, verify_ticket

__all__ = ["Credentials", "mint_ticket", "verify_ticket", "create_app"]


def create_app(*args, **kwargs):
    # Imported lazily: starlette exists only under the [serve] extra.
    from .app import create_app as _create_app

    return _create_app(*args, **kwargs)
