"""The session port: the read, subscribe and act surface a presentation
layer consumes. Everything that crosses it is a value: model dataclasses,
identity keys, status names (see winslow.model). Two classes per scope,
mirroring the serve split (see winslow.client.base).

Each transport module implements the same pair: local.py in-process over
the core, websocket.py over the serve protocol. A future protocol adds a
module, never a new surface."""

from winslow.client.base import AppClient, SessionClient
from winslow.client.local import LocalAppClient, LocalSessionClient

__all__ = [
    "AppClient",
    "SessionClient",
    "LocalAppClient",
    "LocalSessionClient",
]
