import gc
import logging
import re
import textwrap
import threading
import weakref

from argparse import Namespace

import pytest

from winslow.cache import (
    CACHE_LOGGER_NAME,
    GlobalCacheRegistry,
    get_global_cache,
    initialize_global_cache,
    reset_global_cache,
    set_global_cache_registry,
    stray_workflow_caches,
)
from winslow import ConfigOption, Workflow
from winslow.constants import Mode
from winslow.exceptions import InitializationError
from winslow.logger import TASK_LOGGER_NAME
from winslow.util import isolated_scopes

from harness import (
    build_workflow,
    by_name,
    headless_orchestrator,
    run_all,
    workflow_repo,
)


@pytest.fixture
def fresh_global_scope():
    """The global container is process state, and every workflow init in this
    process touched it. Reset around the tests that assert on it."""
    reset_global_cache()
    yield
    reset_global_cache()


def test_cache_feeds_get_parameters_and_tasks(e2e_repo, mode):
    workflow = build_workflow(e2e_repo, "my-cache", mode)
    weather = workflow.workflow_cache.weather

    # Eager fields populated before the graph, in dependency order. The graph
    # exists: get_parameters read the container through the context variable.
    assert weather.loads == ["cities", "city_index"]
    assert sorted(
        t._parameters_dict["city"]
        for t in workflow.tasks
        if t.instance_name == "load-cities"
    ) == ["athens", "bergen", "cairo"]

    run_all(workflow)

    target = workflow.target
    assert target[("loaded", "athens")] == 0
    assert target[("loaded", "bergen")] == 1
    assert target[("loaded", "cairo")] == 2
    assert target[("forecast",)] == ("ATHENS", "BERGEN", "CAIRO")


def test_eager_failure_aborts_the_initialization(e2e_repo):
    with pytest.raises(InitializationError, match=r"'boom_cache\.kaboom'"):
        build_workflow(e2e_repo, "my-cache-boom", Mode.HEADLESS)


def test_workflow_container_dies_with_the_session(e2e_repo):
    workflow = build_workflow(e2e_repo, "my-cache", Mode.TUI)
    weather_ref = weakref.ref(workflow.workflow_cache.weather)

    run_all(workflow)
    workflow.session.end()
    gc.collect()

    assert weather_ref() is None


def test_invalidation_log_lands_in_the_task_log_view(e2e_repo, caplog):
    caplog.set_level(logging.INFO, logger=TASK_LOGGER_NAME)
    workflow = build_workflow(e2e_repo, "my-cache", Mode.TUI)

    run_all(workflow)

    refresher = by_name(workflow)["RefreshForecast"]
    messages = [record.getMessage() for record in refresher.buffered_logs]
    assert (
        "Cache 'weather': invalidate('cities') dropped 'cities', "
        "'city_index', 'forecast'." in messages
    )
    # The record is attributed to the triggering task alone.
    for task in workflow.tasks:
        if task is not refresher:
            assert not any(
                "invalidate('cities')" in record.getMessage()
                for record in task.buffered_logs
            )


def test_access_outside_a_task_scope_logs_to_the_cache_logger(e2e_repo, caplog):
    workflow = build_workflow(e2e_repo, "my-cache", Mode.TUI)

    with caplog.at_level(logging.INFO, logger=CACHE_LOGGER_NAME):
        workflow.workflow_cache.weather.invalidate("cities")

    (record,) = caplog.records
    assert record.name == CACHE_LOGGER_NAME


def test_orchestrator_wires_the_global_scope(e2e_repo, fresh_global_scope):
    orchestrator = headless_orchestrator(e2e_repo, "my-cache")
    assert orchestrator.start() is True

    my_global = get_global_cache().my_global_cache
    # The eager field loaded once, at the first workflow init; the lazy field
    # stays cold.
    assert my_global.loads == ["stations"]
    assert my_global.stations == ("north", "south")


def test_clear_cache_clears_the_global_scope_on_every_init(
    e2e_repo, fresh_global_scope
):
    registry = GlobalCacheRegistry(Namespace())
    registry.collect_classes(e2e_repo)
    set_global_cache_registry(registry)

    first = build_workflow(e2e_repo, "my-cache", Mode.HEADLESS, "--clear-cache")
    my_global = first.global_cache.my_global_cache
    assert my_global.loads == ["stations"]

    # A later init under the flag drops the live values and repopulates the
    # eager fields, so the run starts from cold caches in both scopes.
    build_workflow(e2e_repo, "my-cache", Mode.HEADLESS, "--clear-cache")
    assert my_global.loads == ["stations", "stations"]


def test_concurrent_global_initializations_populate_once(e2e_repo, fresh_global_scope):
    """Two workflows that initialize concurrently share one container and
    trigger the eager population one time (see initialize_global_cache)."""
    registry = GlobalCacheRegistry(Namespace())
    registry.collect_classes(e2e_repo)
    set_global_cache_registry(registry)

    containers = []
    threads = [
        threading.Thread(
            target=lambda: containers.append(initialize_global_cache(Namespace()))
        )
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(containers) == 4
    assert all(container is containers[0] for container in containers)
    assert containers[0].my_global_cache.loads == ["stations"]


def test_global_container_builds_once_per_process(e2e_repo, fresh_global_scope):
    registry = GlobalCacheRegistry(Namespace())
    registry.collect_classes(e2e_repo)
    set_global_cache_registry(registry)

    first = build_workflow(e2e_repo, "my-cache", Mode.HEADLESS)
    second = build_workflow(e2e_repo, "my-cache", Mode.HEADLESS)

    assert first.global_cache is second.global_cache
    assert first.global_cache.my_global_cache.loads == ["stations"]


def _write(path, source):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source))


def test_discovery_imports_only_the_cache_locations(tmp_path):
    _write(
        tmp_path / "cache.py",
        """
        from winslow.cache import GlobalCache, entry

        class RootCache(GlobalCache):
            @entry
            def value(self):
                return 1
        """,
    )
    _write(
        tmp_path / "etl" / "cache" / "extra.py",
        """
        from winslow.cache import GlobalCache

        class PackagedCache(GlobalCache):
            pass
        """,
    )
    _write(
        tmp_path / "etl" / "helpers.py",
        """
        from winslow.cache import GlobalCache

        class IgnoredCache(GlobalCache):
            pass

        raise AssertionError("a module outside the cache locations must not import")
        """,
    )

    with isolated_scopes():
        registry = GlobalCacheRegistry(Namespace())
        registry.collect_classes(str(tmp_path))

    assert registry.names == ["packaged_cache", "root_cache"]


def test_an_abstract_cache_is_not_collected(tmp_path):
    """Meta.abstract works for a cache class the way it does for a workflow
    and a task: the base is skipped, its concrete subclass registers."""
    _write(
        tmp_path / "cache.py",
        """
        from winslow.cache import GlobalCache, entry

        class SharedBase(GlobalCache):
            class Meta:
                abstract = True

            def helper(self):
                return 1

        class Rates(SharedBase):
            @entry
            def value(self):
                return self.helper()
        """,
    )

    with isolated_scopes():
        registry = GlobalCacheRegistry(Namespace())
        registry.collect_classes(str(tmp_path))

    assert registry.names == ["rates"]


def test_clear_cache_option_drops_the_persisted_records(tmp_path, monkeypatch):
    monkeypatch.setenv("WINSLOW_CACHE_DIR", str(tmp_path / "cache-root"))
    loads = tmp_path / "loads.txt"
    loads.write_text("")
    repo = tmp_path / "repo"
    _write(
        repo / "workflows" / "wf" / "workflow.py",
        """
        from winslow import Workflow

        class Wf(Workflow):
            pass
        """,
    )
    _write(
        repo / "workflows" / "wf" / "cache.py",
        f"""
        from pathlib import Path

        from winslow.cache import JsonFileStorage, MemoryStorage, WorkflowCache, compose, entry

        class Persisted(WorkflowCache):
            storage_class = compose(MemoryStorage, JsonFileStorage)

            @entry(eager=True)
            def data(self):
                with Path({str(loads)!r}).open("a") as f:
                    f.write("load\\n")
                return {{"a": 1}}
        """,
    )

    with workflow_repo(repo) as directory:
        build_workflow(directory, "wf", Mode.HEADLESS)  # cold: loads and persists
        build_workflow(directory, "wf", Mode.HEADLESS)  # warm from the file
        build_workflow(directory, "wf", Mode.HEADLESS, "--clear-cache")  # cold again

    assert loads.read_text().count("load") == 2


def test_stray_workflow_caches_are_found(tmp_path):
    _write(
        tmp_path / "workflows" / "etl" / "workflow.py",
        """
        from winslow import Workflow

        class Etl(Workflow):
            pass
        """,
    )
    _write(
        tmp_path / "workflows" / "etl" / "cache.py",
        """
        from winslow.cache import WorkflowCache

        class InsideCache(WorkflowCache):
            pass
        """,
    )
    _write(
        tmp_path / "cache.py",
        """
        from winslow.cache import WorkflowCache

        class StrayCache(WorkflowCache):
            pass
        """,
    )

    with isolated_scopes():
        strays = stray_workflow_caches(
            str(tmp_path), [str(tmp_path / "workflows" / "etl")]
        )

    assert [kls.__name__ for kls in strays] == ["StrayCache"]


class Identified(Workflow):
    """The identity fixture: one scalar and one structured identifier, so the
    prefix keeps the first and the hash covers both."""

    client = ConfigOption(identifier=True, help_text="The client of this run.")
    desks = ConfigOption(
        choices=["fx", "rates", "credit"],
        multiselect=True,
        default=["fx"],
        identifier=True,
        help_text="The desks of this run.",
    )


def _identified(**values):
    """The identity properties read only the class meta and the config, so the
    fixture skips the runner and store construction."""
    workflow = object.__new__(Identified)
    workflow.workflow_config = Namespace(**values)
    return workflow


def test_identity_prefix_is_readable_and_lossy():
    workflow = _identified(client="Acme Corp", desks=["fx", "rates"])
    # The scalar identifier reads back slugged; the list identifier is dropped.
    assert workflow.identity_prefix == "identified-acme-corp"


def test_identity_hash_covers_what_the_prefix_drops():
    one = _identified(client="acme", desks=["fx"])
    two = _identified(client="acme", desks=["fx", "rates"])
    assert one.identity_prefix == two.identity_prefix
    assert one.identity_hash != two.identity_hash
    assert one.cache_namespace != two.cache_namespace


def test_cache_namespace_is_stable_per_identity():
    one = _identified(client="acme", desks=["fx"])
    again = _identified(client="acme", desks=["fx"])
    assert one.cache_namespace == again.cache_namespace
    assert re.fullmatch(r"identified-acme-[0-9a-f]{8}", one.cache_namespace)


def test_workflow_stamps_the_namespace_onto_its_config(e2e_repo):
    workflow = build_workflow(e2e_repo, "my-cache", Mode.HEADLESS)
    assert workflow.workflow_config.cache_namespace == workflow.cache_namespace
    assert re.fullmatch(r"my-cache-[0-9a-f]{8}", workflow.cache_namespace)
