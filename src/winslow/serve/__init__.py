"""The serve process: one websocket endpoint over the live sessions of a
SessionRegistry (see specs on the session bus and the action handler for the
two halves it exposes). Requires the [serve] extra."""

from .auth import Credentials, mint_ticket, verify_ticket

__all__ = ["Credentials", "mint_ticket", "verify_ticket", "create_app"]


def create_app(*args, **kwargs):
    # Imported lazily: starlette exists only under the [serve] extra.
    from .app import create_app as _create_app

    return _create_app(*args, **kwargs)
