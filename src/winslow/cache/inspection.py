from dataclasses import dataclass
from enum import StrEnum
from typing import Optional


class EntryState(StrEnum):
    """The freshness of one entry, derived at peek time (see BaseCache.inspect)."""

    COLD = "cold"
    WARM = "warm"
    STALE = "stale"
    # A loader produces the value right now. A live observation only: the
    # state never appears in a history snapshot.
    COMPUTING = "computing"
    # A delete or a loader failed on the entry. The error context of the
    # projection names the operation and the layer (see CacheEntryError).
    ERRORED = "errored"


# States with a trustworthy value: ERRORED can carry a leftover one.
PREVIEWABLE_STATES = frozenset((EntryState.WARM, EntryState.STALE))


class ErrorOrigin(StrEnum):
    """The operation that left an entry in the ERRORED state."""

    DELETE = "delete"
    LOAD = "load"


@dataclass(frozen=True)
class CacheEntryError:
    """The error context of one entry: the failed operation and its layer.
    Plain strings only, so the context stays wire-ready. A successful write
    of the entry clears it (see BaseCache._entry_value)."""

    origin: ErrorOrigin
    tier: Optional[str]  # the failing storage layer; None for a loader error
    message: str
    at: float
    # The traceback, formatted at failure time: a string retains no frame,
    # so the context stays GC-safe and a value view can show the full cause.
    traceback: Optional[str] = None


class SnapshotEncoding(StrEnum):
    """What the rendered field of a snapshot holds. TEXT displays as it is;
    JSON deserializes back into a value for a tree view."""

    TEXT = "text"
    JSON = "json"


@dataclass(frozen=True)
class CacheReadSnapshot:
    """One recorded cache read of a task phase, rendered and bounded. Plain
    strings only, so a history record outlives the session and its caches.
    A summary marks a bounded rendering; a full rendering carries none."""

    scope: str
    cache_name: str
    entry_name: str
    written_at: float
    rendered: str
    summary: Optional[str]
    encoding: SnapshotEncoding


@dataclass(frozen=True)
class CacheEntryInfo:
    """The projection of one cache entry for a UI layer. Plain scalar fields
    only, so the projection is wire-ready. It never carries the value."""

    scope: str
    cache_name: str
    entry_name: str
    state: EntryState
    written_at: Optional[float]
    ttl: Optional[float]
    eager: bool
    depends_on: tuple
    storage: str
    error: Optional[CacheEntryError]
