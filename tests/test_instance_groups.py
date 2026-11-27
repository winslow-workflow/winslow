import pytest

from winslow.constants import Mode
from winslow.model import TaskInfo

from harness import build_workflow, by_params

REGIONS = ("eu", "us", "ap")


@pytest.fixture
def groups_workflow(e2e_repo, mode):
    return build_workflow(e2e_repo, "my-groups", mode)


def test_instance_holds_the_group_of_its_own_parameters(groups_workflow):
    """get_groups receives the parameters of the instance, so each instance of
    one class holds a group of its own."""
    tasks = by_params(groups_workflow)

    for region in REGIONS:
        assert tasks[("Ship", region)].instance_groups == {f"ship-{region}"}


def test_class_groups_stay_the_groups_without_parameters(groups_workflow):
    """The call with no parameters still answers for the class."""
    tasks = by_params(groups_workflow)

    assert type(tasks[("Ship", "eu")]).get_groups() == {"ship"}


def test_default_instance_groups_are_the_declared_groups(groups_workflow):
    """A task that overrides nothing holds its declared groups, on the class
    and on the instance."""
    pack = by_params(groups_workflow)[("Pack",)]

    assert pack.instance_groups == type(pack).get_groups() == {"prepare"}
    assert pack.groups_readable == "prepare"


def test_group_filter_selects_one_instance(groups_workflow):
    """The group filter reads the instance groups, so the group of one region
    selects one instance of the family."""
    tasks = by_params(groups_workflow)

    assert groups_workflow.filter_keys("!g ship-us") == (
        tasks[("Ship", "us")].identity_key,
    )
    assert groups_workflow.filter_keys("!g ship") == ()


def test_filter_option_runs_one_instance(e2e_repo):
    """The --filter option of a headless run takes the group of one instance."""
    workflow = build_workflow(
        e2e_repo, "my-groups", Mode.HEADLESS, "--filter", "!g ship-eu"
    )
    tasks = by_params(workflow)

    workflow.headless_run()

    assert tasks[("Ship", "eu")] in workflow.target
    assert tasks[("Ship", "us")] not in workflow.target
    assert tasks[("Ship", "ap")] not in workflow.target


def test_group_dependency_resolves_against_the_class_groups(groups_workflow):
    """A dependency on a group name reads the groups of the class, so it
    reaches every instance of the family."""
    tasks = by_params(groups_workflow)

    assert set(tasks[("Notify",)].dependent_tasks) == {
        tasks[("Ship", region)] for region in REGIONS
    }


def test_task_info_carries_the_instance_groups(groups_workflow):
    """The info a client reads, over the wire or in the UI, names the groups
    of the instance, and the group filter reads an info like a task."""
    ship = by_params(groups_workflow)[("Ship", "ap")]
    info = TaskInfo.from_task(ship)

    assert info.groups == ("ship-ap",)
    assert info.instance_groups == ship.instance_groups
    assert info.groups_readable == "ship-ap"
