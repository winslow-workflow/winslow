"""An autoloaded plugin can be turned off via [tool.winslow] disabled_tui_plugins -
by qualified name or by whole source dist."""

from winslow.ui.plugin import Slots


def _names(reg, slot):
    return [p.get_name() for p in reg.for_slot(slot)]


def test_disabled_by_qualified_name(make_registry, plugins_package):
    reg = make_registry(tool_winslow={"disabled_tui_plugins": ["builtin.alpha"]})
    reg.discover(plugins_package)
    assert _names(reg, Slots.TASK_OVERVIEW) == ["beta"]


def test_disabled_by_source(make_registry, install_entrypoint):
    install_entrypoint(dist="acme-plugins")
    reg = make_registry(tool_winslow={"disabled_tui_plugins": ["acme-plugins"]})
    reg.discover_installed()
    assert _names(reg, Slots.TASK_OVERVIEW) == []
