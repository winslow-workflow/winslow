import json
import time

from argparse import Namespace

import pytest

from winslow.cache import (
    BaseStorage,
    ComposedStorage,
    GlobalCache,
    JsonFileStorage,
    MemoryStorage,
    StorageRecord,
    WorkflowCache,
    compose,
    entry,
)
from winslow.exceptions import MisconfigurationError, SerializationError


@pytest.fixture
def tmp_json_storage(tmp_path):
    class TmpJsonStorage(JsonFileStorage):
        base_directory = tmp_path

    return TmpJsonStorage


def build_cache(storage, loader, loads=None):
    """One cache class with one entry, on the given storage. `loads` records
    the loader calls. The namespace is the stamp a real workflow provides."""

    class Built(WorkflowCache):
        name = "built"
        storage_class = storage

        @entry
        def values(self):
            if loads is not None:
                loads.append("values")
            return loader()

    return Built(Namespace(cache_namespace="wf-00000000"))


def test_json_write_normalizes_from_the_first_read(tmp_json_storage):
    cache = build_cache(tmp_json_storage, lambda: (("a", 1), ("b", 2)))
    # The JSON round trip is served immediately, never only after a restart.
    assert cache.values == [["a", 1], ["b", 2]]


def test_json_layout_and_warm_start(tmp_path, tmp_json_storage):
    loads = []
    first = build_cache(tmp_json_storage, lambda: {"oslo": 1}, loads)
    assert first.values == {"oslo": 1}

    payload = json.loads(
        (tmp_path / "workflows" / "wf-00000000" / "built" / "values.json").read_text()
    )
    assert payload["value"] == {"oslo": 1}

    # A fresh instance reads the file: the loader does not run again.
    second = build_cache(tmp_json_storage, lambda: {"oslo": 1}, loads)
    assert second.values == {"oslo": 1}
    assert loads == ["values"]


def test_json_rejects_a_non_serializable_value(tmp_json_storage):
    cache = build_cache(tmp_json_storage, lambda: object())
    with pytest.raises(SerializationError, match="'built', entry 'values'"):
        cache.values


def test_a_corrupt_file_reads_as_cold(tmp_path, tmp_json_storage):
    loads = []
    build_cache(tmp_json_storage, lambda: {"a": 1}, loads).values
    (tmp_path / "workflows" / "wf-00000000" / "built" / "values.json").write_text(
        "not json"
    )

    assert build_cache(tmp_json_storage, lambda: {"a": 1}, loads).values == {"a": 1}
    assert loads == ["values", "values"]


def test_compose_promotes_and_serves_from_memory(tmp_path, tmp_json_storage):
    loads = []
    storage = compose(MemoryStorage, tmp_json_storage)

    writer = build_cache(storage, lambda: {"a": 1}, loads)
    assert writer.values == {"a": 1}  # computes, write-through to both tiers

    reader = build_cache(storage, lambda: {"a": 1}, loads)
    assert reader.values == {"a": 1}  # file hit, promoted into memory

    (tmp_path / "workflows" / "wf-00000000" / "built" / "values.json").unlink()
    assert reader.values == {"a": 1}  # the memory tier serves alone

    assert loads == ["values"]


def test_promotion_keeps_the_write_time(tmp_path, tmp_json_storage):
    directory = tmp_path / "workflows" / "wf-00000000" / "built"
    directory.mkdir(parents=True)
    (directory / "values.json").write_text(
        json.dumps({"written_at": 123.0, "value": {"a": 1}})
    )

    cache = build_cache(compose(MemoryStorage, tmp_json_storage), lambda: {"a": 1})
    assert cache.values == {"a": 1}

    memory_tier = cache._storage._tiers[0]
    assert memory_tier.read("values").written_at == 123.0


def test_compose_writes_the_normalized_value_into_every_tier(tmp_json_storage):
    cache = build_cache(compose(MemoryStorage, tmp_json_storage), lambda: ("a", "b"))
    assert cache.values == ["a", "b"]
    # Bottom-up write: the memory tier holds the JSON round trip too.
    assert cache._storage._tiers[0].read("values").value == ["a", "b"]


def test_invalidate_clears_every_writable_tier(tmp_path, tmp_json_storage):
    loads = []
    cache = build_cache(
        compose(MemoryStorage, tmp_json_storage), lambda: {"a": 1}, loads
    )
    cache.values

    cache.invalidate("values")

    assert not (
        tmp_path / "workflows" / "wf-00000000" / "built" / "values.json"
    ).exists()
    assert cache.values == {"a": 1}
    assert loads == ["values", "values"]


def test_a_read_only_tier_is_skipped_by_write_and_delete():
    class SourceStorage:
        """A backoff source with no write and no delete: calling either would
        raise AttributeError, so a skip is proven by the absence of an error."""

        read_only = True

        def __init__(self, cache_name, namespace):
            self.reads = 0

        def read(self, key):
            self.reads += 1
            return StorageRecord(value={"seed": True}, written_at=time.time())

    cache = build_cache(compose(MemoryStorage, SourceStorage), lambda: {"never": True})

    assert cache.values == {"seed": True}  # served by the source, not the loader
    cache.invalidate("values")  # clears the memory tier only
    assert cache.values == {"seed": True}  # re-read through the source

    source = cache._storage._tiers[1]
    assert source.reads == 2


def test_same_named_caches_keep_separate_files(tmp_path, tmp_json_storage):
    """The scope namespace keeps same-named caches apart, and the workflows/
    segment keeps even a workflow named "global" out of the global scope."""

    class GlobalStations(GlobalCache):
        name = "stations"
        storage_class = tmp_json_storage

        @entry
        def values(self):
            return "process"

    class WorkflowStations(WorkflowCache):
        name = "stations"
        storage_class = tmp_json_storage

        @entry
        def values(self):
            return self.workflow_config.cache_namespace

    assert GlobalStations(Namespace()).values == "process"
    etl = WorkflowStations(Namespace(cache_namespace="etl-1a2b3c4d"))
    assert etl.values == "etl-1a2b3c4d"
    assert WorkflowStations(Namespace(cache_namespace="global")).values == "global"

    layout = sorted(
        p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.json")
    )
    assert layout == [
        "global/stations/values.json",
        "workflows/etl-1a2b3c4d/stations/values.json",
        "workflows/global/stations/values.json",
    ]


def test_compose_validates_its_tiers():
    with pytest.raises(MisconfigurationError, match="at least one"):
        compose()
    assert compose(MemoryStorage) is MemoryStorage
    # A real class, never a partial: a partial as a class attribute binds the
    # instance on Python 3.14 and corrupts the constructor call.
    assert issubclass(compose(MemoryStorage, JsonFileStorage), ComposedStorage)


def test_builtin_backends_share_the_base_contract():
    """BaseStorage states the seam: the built-ins subclass it, and it carries
    the constructor and the read_only default."""
    for kls in (MemoryStorage, JsonFileStorage, ComposedStorage):
        assert issubclass(kls, BaseStorage)
    backend = MemoryStorage("built", "wf-00000000")
    assert (backend.cache_name, backend.namespace) == ("built", "wf-00000000")
    assert backend.read_only is False


def test_file_storage_rejects_a_path_escaping_namespace(tmp_json_storage):
    """The storage validates its own path components: a forged stamp must not
    write or delete outside the cache root."""
    for namespace in ("../../outside", "/abs", "a\\b", "workflows//x", ".."):
        with pytest.raises(MisconfigurationError, match="illegal storage path"):
            tmp_json_storage("stations", namespace)
    # The two framework shapes pass.
    tmp_json_storage("stations", "global")
    tmp_json_storage("stations", "workflows/etl-eu-3f9a1c2b")
