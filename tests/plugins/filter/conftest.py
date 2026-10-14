import pytest

from winslow.conf import settings
from winslow.constants import Mode

from harness import COMPLETED_LADDER, UNPROCESSED, build_workflow, by_name

FLAVOR_TASKS = ("Sweet", "Sour", "Salty", "Bitter")


FLAVOR = "plugins.filter.my_filters.filters.FlavorFilter"
TAG = "plugins.filter.my_filters.filters.TagFilter"
DUP_ONE = "plugins.filter.my_filters_clash.filters.DupOne"
DUP_TWO = "plugins.filter.my_filters_clash.filters.DupTwo"


@pytest.fixture
def list_filters(monkeypatch):
    """Set FILTER_PLUGINS to the given paths, the way a project module does.
    Call it before build_workflow, since FilterRegistry is built in
    Workflow.__init__; everything downstream (resolve, parse, the filtered
    run) stays real."""

    def _list(*paths):
        monkeypatch.setattr(settings, "FILTER_PLUGINS", list(paths))

    return _list


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
