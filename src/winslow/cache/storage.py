import json
import os
import re
import tempfile

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from winslow.exceptions import (
    DeserializationError,
    MisconfigurationError,
    SerializationError,
    StorageError,
)
from winslow.logger import LOGGER
from winslow.settings import config


# The sentinel for a key with no record. None cannot mark it, because None is
# a legal cached value.
MISSING = object()


# One path component under the cache root. No leading dot and no separator,
# so a "..", a hidden name and an absolute path all fail.
_PATH_COMPONENT_PATTERN = re.compile(r"[a-z0-9_-][a-z0-9_.-]*\Z")


def _validate_path_components(cache_name, namespace):
    """Reject a namespace or name that could escape the cache root. The
    framework stamps are safe by construction; this guards a hand-built one."""
    for component in (cache_name, *str(namespace).split("/")):
        if not _PATH_COMPONENT_PATTERN.match(component):
            raise MisconfigurationError(
                f"Cache '{cache_name}': illegal storage path component "
                f"{component!r} in namespace {namespace!r} - a component "
                f"must match [a-z0-9_-][a-z0-9_.-]*."
            )


@dataclass(frozen=True)
class StorageRecord:
    """One stored value with its write time. The Entry descriptor builds the
    record and owns the expiry decision, so every backend stays dumb."""

    value: Any
    written_at: float


class BaseStorage:
    """The storage contract of a cache. The built-in backends subclass it; a
    custom backend overrides the three methods (see docs/caching.md)."""

    # A read-only backend is a pure source: ComposedStorage skips it on write
    # and delete.
    read_only = False

    def __init__(self, cache_name, namespace):
        self.cache_name = cache_name
        self.namespace = namespace

    def read(self, key):
        """Return the StorageRecord of the key, or the MISSING sentinel. None
        cannot mark a miss: it is a legal cached value."""
        raise NotImplementedError

    def peek(self, key):
        """Return the record of the key, or MISSING, with no side effect. The
        default delegates to read; a backend whose read has a side effect
        overrides this, so an observation never changes the stored state."""
        return self.read(key)

    def describe(self):
        """The human label of the backend for the UI projections."""
        return type(self).__name__

    def write(self, key, record):
        """Store the record and return the stored record. The caller serves
        the returned value, so a normalizing backend returns its round trip."""
        raise NotImplementedError

    def delete(self, key):
        """Remove the record of the key, also when the key holds no record."""
        raise NotImplementedError


class MemoryStorage(BaseStorage):
    """The default backend: a dict per cache instance. It puts no constraint
    on the values."""

    def __init__(self, cache_name, namespace):
        super().__init__(cache_name, namespace)
        self._records = {}

    def read(self, key):
        """Return the record of the key, or MISSING."""
        return self._records.get(key, MISSING)

    def write(self, key, record):
        """Store the record verbatim and return it."""
        self._records[key] = record
        return record

    def delete(self, key):
        """Remove the record of the key. An absent key is a no-op."""
        self._records.pop(key, None)


class JsonFileStorage(BaseStorage):
    """One JSON file per entry, under a directory per scope and cache. The
    write is strict and atomic; a corrupt or unreadable file reads as MISSING."""

    # None resolves to the WINSLOW_CACHE_DIR setting. A test or a project can
    # override it on a subclass. The default is relative to the CWD.
    base_directory = None

    def __init__(self, cache_name, namespace):
        _validate_path_components(cache_name, namespace)
        super().__init__(cache_name, namespace)
        base = self.base_directory or config(
            "WINSLOW_CACHE_DIR", default=".winslow/cache"
        )
        # The namespace keeps same-named caches of different scopes apart.
        self.directory = Path(base) / namespace / cache_name

    def _path(self, key):
        return self.directory / f"{key}.json"

    def read(self, key):
        try:
            return self._decode(self._path(key).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return MISSING
        except (OSError, DeserializationError):
            LOGGER.error(
                f"Cache file {self._path(key)} is unreadable - "
                f"treating the entry as cold.",
                exc_info=True,
            )
            return MISSING

    @classmethod
    def _decode(cls, text):
        """Decode one stored record. A corrupt payload raises, and the caller
        owns the policy: read() serves a cold miss instead."""
        try:
            payload = json.loads(text)
            return StorageRecord(
                value=payload["value"], written_at=payload["written_at"]
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise DeserializationError(
                f"the record is not a valid cache payload ({exc})"
            ) from exc

    def write(self, key, record):
        """Store the record and return its JSON round trip: the caller serves
        the normalized value, so a value never changes shape after a restart."""
        try:
            text = json.dumps({"written_at": record.written_at, "value": record.value})
        except TypeError as exc:
            # Strict on purpose: a default=str fallback would coerce silently,
            # and a lossy cache file is not debuggable.
            raise SerializationError(
                f"Cache '{self.cache_name}', entry '{key}': "
                f"the value is not JSON-serializable ({exc})."
            ) from exc
        self.directory.mkdir(parents=True, exist_ok=True)
        # A private temp name per writer: two processes that write the same
        # entry cannot publish each other's bytes through a shared temp file.
        with tempfile.NamedTemporaryFile(
            "w", dir=self.directory, suffix=".json.tmp", delete=False, encoding="utf-8"
        ) as temp:
            temp.write(text)
        os.replace(temp.name, self._path(key))
        return StorageRecord(
            value=json.loads(text)["value"], written_at=record.written_at
        )

    def delete(self, key):
        self._path(key).unlink(missing_ok=True)


class ComposedStorage(BaseStorage):
    """Tiered storage: declaration order is read order. A tier that declares
    read_only = True is skipped by write and delete."""

    # The tier classes. compose() declares them on a subclass.
    storage_classes = ()

    def __init__(self, cache_name, namespace):
        super().__init__(cache_name, namespace)
        self._tiers = [kls(cache_name, namespace) for kls in self.storage_classes]
        self._writable = [tier for tier in self._tiers if not tier.read_only]

    def read(self, key):
        """The first hit wins. It is promoted verbatim into the writable tiers
        above it, write time included, so a ttl does not reset on promotion."""
        for index, tier in enumerate(self._tiers):
            record = tier.read(key)
            if record is not MISSING:
                for upper in self._tiers[:index]:
                    if upper in self._writable:
                        upper.write(key, record)
                return record
        return MISSING

    def peek(self, key):
        """The first hit wins, without the promotion of read: an observation
        must not change the tiers."""
        for tier in self._tiers:
            record = tier.peek(key)
            if record is not MISSING:
                return record
        return MISSING

    def describe(self):
        """The tier labels in read order, joined for the UI. The generated
        class is always named ComposedStorage, so the label uses the tiers."""
        return " over ".join(tier.describe() for tier in self._tiers)

    def write(self, key, record):
        """Bottom-up: every tier stores what the tier below persisted, so each
        tier serves the most constrained representation."""
        for tier in reversed(self._writable):
            record = tier.write(key, record)
        return record

    def delete(self, key):
        """Attempt every tier, then raise one StorageError naming each
        failing tier: a raise mid-loop would leave the tiers below in a
        silent, half-deleted state. The cache quarantines the entry."""
        failures = [
            (tier.describe(), exc)
            for tier in self._writable
            if (exc := _tier_delete_error(tier, key)) is not None
        ]
        if failures:
            details = "; ".join(
                f"{label}: {type(exc).__name__}: {exc}" for label, exc in failures
            )
            raise StorageError(
                f"Cache '{self.cache_name}', entry '{key}': "
                f"delete failed on {details}.",
                tiers=[label for label, _ in failures],
            )


def _tier_delete_error(tier, key):
    """The exception of one tier's delete, or None. Every tier must run
    before the aggregate raises (see ComposedStorage.delete)."""
    try:
        tier.delete(key)
    except Exception as exc:
        return exc
    return None


def compose(*storage_classes):
    """A storage class with the given tiers, read order first to last (see
    docs/caching.md)."""
    if not storage_classes:
        raise MisconfigurationError("compose() needs at least one storage class.")
    if len(storage_classes) == 1:
        return storage_classes[0]
    # A real class, never a partial: functools.partial binds the instance as
    # a descriptor on Python 3.14, which breaks the class-attribute call.
    return type(
        "ComposedStorage", (ComposedStorage,), {"storage_classes": storage_classes}
    )
