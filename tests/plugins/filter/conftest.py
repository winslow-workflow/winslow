import importlib
from types import SimpleNamespace

import pytest

from winslow import autodiscovery
from winslow.constants import Mode

from harness import COMPLETED_LADDER, UNPROCESSED, build_workflow, by_name

FLAVOR_TASKS = ("Sweet", "Sour", "Salty", "Bitter")


class FakeEntryPoint:
    """Duck-types importlib.metadata.EntryPoint for discover_installed: .load()
    returns the already-importable test package, .dist.name is the source dist
    the qualified names key off."""

    def __init__(self, package, dist):
        self._package = package
        self.dist = SimpleNamespace(name=dist)

    def load(self):
        return self._package


@pytest.fixture
def install_filters(monkeypatch):
    """Make FilterRegistry discover tests/plugins/filter/my_filters as if it were an
    installed `winslow.filter_plugins` dist, with an optional [tool.winslow] config.
    Patches the two seams a real install would provide; everything downstream
    (guard chain, resolve, parse, filtered run) stays real. Must be called
    before build_workflow, since FilterRegistry is built in Workflow.__init__."""

    def _install(
        package="plugins.filter.my_filters", tool_winslow=None, dist="acme-filters"
    ):
        entry_point = FakeEntryPoint(importlib.import_module(package), dist)
        monkeypatch.setattr(
            autodiscovery,
            "entry_points",
            lambda group: [entry_point] if group == "winslow.filter_plugins" else [],
        )
        monkeypatch.setattr(
            autodiscovery, "_winslow_pyproject_table", lambda cwd: tool_winslow or {}
        )

    return _install


@pytest.fixture
def build_filtered(e2e_repo):
    """Build the my-filters workflow headless with the given CLI argv (e.g.
    "--filter", "!flavor sweet"). Returns the workflow un-run, so error-path
    tests can assert on build (clashes) or on headless_run (bad filters)."""

    def _build(*argv):
        return build_workflow(e2e_repo, "my-filters", Mode.HEADLESS, *argv)

    return _build


@pytest.fixture
def assert_only_ran():
    """Assert exactly `ran` completed and every other flavor task got only the
    eligibility pre-pass - headless runs eligibility on all tasks but run() on
    the filtered set alone, so a filtered-out task's history stays UNPROCESSED."""

    def _assert(workflow, ran):
        tasks = by_name(workflow)
        for name in FLAVOR_TASKS:
            if name in ran:
                workflow.store.assert_history_equals(tasks[name], COMPLETED_LADDER)
                assert tasks[name] in workflow.target
            else:
                workflow.store.assert_history_equals(tasks[name], UNPROCESSED)
                assert tasks[name] not in workflow.target

    return _assert
