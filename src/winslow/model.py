"""Port DTOs and frame envelopes: the value shapes that cross the serve
boundary (the payload rule, see winslow.events). Every field is JSON-safe.
winslow.codec encodes and decodes these classes.

The existing payload dataclasses (BatchInfo, TaskInfo, CacheEntryInfo,
StatusSnapshot, ...) stay where they are declared today; a later change can
move them here. This module holds only the inbound frame envelopes: they
replace a trusted frame.get(...) read at the serve edge."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ActionFrame:
    """One inbound action frame, decoded at the serve edge before dispatch
    (see winslow.serve.wire.build_action)."""

    type: str
    session_id: str
    action: str
    request_id: str | None = None
    fields: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RequestFrame:
    """One inbound request frame, decoded at the serve edge. Every request
    kind shares this envelope; a handler reads only the fields its kind
    needs - the rest stay None."""

    type: str
    kind: str
    request_id: str | None = None
    session_id: str | None = None
    workflow: str | None = None
    overrides: dict | None = None
    values: dict | None = None
    batch_uuid: str | None = None
    task_key: str | None = None
    limit: int | None = None
    query: str | None = None
    builtin_only: bool = False
    cache_name: str | None = None
    entry_name: str | None = None
