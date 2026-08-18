"""History capture of the cache reads of one task phase. The interactive
runner installs the proxies per phase and sweeps the recorder into snapshots
(see InteractiveRunner.task_scope)."""

import json
import pprint

from collections.abc import Sized
from contextlib import contextmanager

from winslow import settings
from winslow.cache.base import BaseCache, DisplayStyle, declared_entries
from winslow.cache.inspection import CacheReadSnapshot, SnapshotEncoding
from winslow.cache.storage import StorageRecord


def resolve_snapshot_cap(cache_class):
    """The snapshot cap of a cache class in bytes. None resolves against the
    settings module at call time, so a test patches the module constant."""
    cap = cache_class.snapshot_size_bytes
    return settings.CACHE_SNAPSHOT_SIZE_BYTES if cap is None else cap


class CacheReadRecorder:
    """The reads of one execution phase. The last read of an entry wins, and
    sweep drops every held reference: history outlives the caches."""

    def __init__(self):
        self._reads = {}

    def record(self, cache, entry_name, record):
        key = (cache.scope, cache.get_name(), entry_name)
        self._reads[key] = (cache, record)

    def sweep(self):
        """Render every recorded read into a snapshot and drop the references."""
        snapshots = tuple(
            _render_read(key, cache, record)
            for key, (cache, record) in self._reads.items()
        )
        self._reads.clear()
        return snapshots


class _RecordingBase:
    def __init__(self, wrapped, recorder):
        object.__setattr__(self, "_wrapped", wrapped)
        object.__setattr__(self, "_recorder", recorder)

    def __setattr__(self, name, value):
        # Delegate, so the read-only contract of the target raises as usual.
        setattr(self._wrapped, name, value)

    def __repr__(self):
        return repr(self._wrapped)


class RecordingCacheContainer(_RecordingBase):
    """A per-phase wrapper over a container: a cache attribute serves a
    recording cache wrapper, every other member passes through."""

    def __getattr__(self, name):
        value = getattr(self._wrapped, name)
        if isinstance(value, BaseCache):
            return RecordingCache(value, self._recorder)
        return value


class RecordingCache(_RecordingBase):
    """A per-phase wrapper over one cache: an entry access serves the real
    value and records the storage record behind it."""

    def __getattr__(self, name):
        value = getattr(self._wrapped, name)
        if name in self._wrapped._entries:
            # A racing invalidation or recompute peeks MISSING or COMPUTING;
            # that read stays out of history.
            try:
                record = self._wrapped.peek(name)
            except Exception:
                # The recorder observes: a failing peek must not turn a
                # successful task read into a task failure.
                self._wrapped.logger.error(
                    f"Cache '{self._wrapped}': the history recorder cannot "
                    f"peek '{name}'.",
                    exc_info=True,
                )
                return value
            if isinstance(record, StorageRecord):
                self._recorder.record(self._wrapped, name, record)
        return value


@contextmanager
def recording_cache_reads(task):
    """Swap the cache stamps of the task for the recording proxies, and
    restore them on exit. A nested scope wraps the original container, so
    each phase records only its own reads."""
    recorder = CacheReadRecorder()
    saved = (task._workflow_cache_container, task._global_cache_container)
    task._workflow_cache_container = _wrap(saved[0], recorder)
    task._global_cache_container = _wrap(saved[1], recorder)
    try:
        yield recorder
    finally:
        task._workflow_cache_container, task._global_cache_container = saved


def _wrap(stamp, recorder):
    if stamp is None:
        return None
    if isinstance(stamp, RecordingCacheContainer):
        stamp = stamp._wrapped
    return RecordingCacheContainer(stamp, recorder)


def _render_read(key, cache, record):
    scope, cache_name, entry_name = key
    display_style = declared_entries(type(cache))[entry_name].display_style
    try:
        rendered, summary, encoding = _render_value(
            record.value, resolve_snapshot_cap(type(cache)), display_style
        )
    except Exception as exc:
        # The sweep runs in the runner's cleanup path: one bad value must
        # degrade one snapshot, never the phase.
        cache.logger.error(
            f"Cache '{cache}': the value of '{entry_name}' cannot render for history.",
            exc_info=True,
        )
        rendered = ""
        summary = f"<unrepresentable: {type(exc).__name__}: {exc}>"
        encoding = SnapshotEncoding.TEXT
    return CacheReadSnapshot(
        scope=scope,
        cache_name=cache_name,
        entry_name=entry_name,
        written_at=record.written_at,
        rendered=rendered,
        summary=summary,
        encoding=encoding,
    )


# The builtin containers render at least one byte per element, so a length
# over the cap proves the rendering exceeds it. Other types stay out: an
# object can pair a large __len__ with a small repr (a dataframe).
_SIZED_BUILTINS = (str, bytes, dict, list, tuple, set, frozenset)


def _render_value(value, cap, display_style):
    """One value as (rendered, summary, encoding), bounded by the cap. Over
    the cap: the head of the rendering, plus the structural summary."""
    if cap <= 0:
        return "", _summarize(value), SnapshotEncoding.TEXT
    if callable(display_style):
        rendered, summary = _capped(str(display_style(value)), cap, value)
        return rendered, summary, SnapshotEncoding.TEXT
    # The length alone proves the overrun: pformat and dumps build the full
    # text before any cut, and a large value pays that in every sweep.
    if isinstance(value, _SIZED_BUILTINS) and len(value) > cap:
        return "", _summarize(value), SnapshotEncoding.TEXT
    if display_style is DisplayStyle.TREE:
        try:
            rendered, summary = _capped(json.dumps(value), cap, value)
        except (TypeError, ValueError):
            # Not a JSON value: the text fallback keeps the entry readable.
            rendered, summary = _capped(pprint.pformat(value), cap, value)
            return rendered, summary, SnapshotEncoding.TEXT
        # A cut JSON string does not parse: only a whole one keeps the tree.
        encoding = SnapshotEncoding.TEXT if summary else SnapshotEncoding.JSON
        return rendered, summary, encoding
    rendered, summary = _capped(pprint.pformat(value), cap, value)
    return rendered, summary, SnapshotEncoding.TEXT


def _capped(text, cap, value):
    """The text under the cap with None, or its head with the summary."""
    encoded = text.encode("utf-8")
    if len(encoded) <= cap:
        return text, None
    return encoded[:cap].decode("utf-8", errors="ignore"), _summarize(value)


def _summarize(value):
    """The structural summary of a value: type, length and a key sample."""
    label = type(value).__name__
    match value:
        case dict() if value:
            keys = ", ".join(repr(key) for key in list(value)[:5])
            return f"{label}, len {len(value)}, keys: {keys}"
        case Sized():
            return f"{label}, len {len(value)}"
        case _:
            return label
