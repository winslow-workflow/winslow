"""UI plugins register two ways: the built-ins by walking a package, the
plugins of a project through the TUI_PLUGINS list. for_slot returns the
registered classes sorted (priority, name)."""

import pytest

from winslow.exceptions import MisconfigurationError, PluginError
from winslow.ui.plugin import Slots

from .conftest import ALPHA, BETA, LOGS


def _names(plugins):
    return [p.get_name() for p in plugins]


def test_the_builtin_package_registers_and_orders(make_registry, plugins_package):
    reg = make_registry()
    reg.register_package(plugins_package)

    # priority 3 (beta) sorts before priority 5 (alpha)
    assert _names(reg.for_slot(Slots.TASK_OVERVIEW)) == ["beta", "alpha"]
    # two plugins in the slot -> the slot renders tabbed
    assert reg.any_tabbed(None, Slots.TASK_OVERVIEW) is True


def test_a_listed_plugin_registers_and_an_unlisted_one_does_not(
    make_registry, list_plugins
):
    from winslow.conf import settings

    list_plugins(BETA, ALPHA)
    reg = make_registry()
    reg.load(settings.TUI_PLUGINS)

    assert _names(reg.for_slot(Slots.TASK_OVERVIEW)) == ["beta", "alpha"]
    assert _names(reg.for_slot(Slots.WORKFLOW_LOGS)) == []


def test_a_listed_path_that_is_not_a_plugin_or_does_not_import_refuses(make_registry):
    reg = make_registry()
    with pytest.raises(PluginError, match="TUI_PLUGINS: .* is not a UIPlugin class"):
        reg.load(["winslow.state.FileStateStore"])
    with pytest.raises(MisconfigurationError, match="does not import"):
        reg.load(["no.such.Plugin"])


def test_a_plugin_of_another_slot_registers_and_stays_out_of_this_one(make_registry):
    reg = make_registry()
    reg.load([LOGS, ALPHA])

    assert _names(reg.for_slot(Slots.TASK_OVERVIEW)) == ["alpha"]
    assert _names(reg.for_slot(Slots.WORKFLOW_LOGS)) == ["logs"]


def test_no_slot_plugin_is_not_a_candidate(make_registry, plugins_package):
    reg = make_registry()
    reg.register_package(plugins_package)

    # NoSlotPlugin has slot=None, so _is_candidate drops it. Candidacy has no
    # public read (for_slot needs a slot), so this checks the registration list.
    assert set(_names(reg._plugins)) == {"alpha", "beta", "logs"}
