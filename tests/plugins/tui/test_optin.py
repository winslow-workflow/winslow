"""An autoload=False plugin is discovered but not registered unless opted in via
[tool.winslow] enabled_tui_plugins - by qualified name or by whole source dist."""

from winslow.ui.plugin import Slots


def _optin_present(reg):
    return "optin" in [p.get_name() for p in reg.for_slot(Slots.WORKFLOW_LOGS)]


def test_optin_absent_by_default(make_registry, plugins_package):
    reg = make_registry()
    reg.discover(plugins_package)
    assert not _optin_present(reg)


def test_optin_enabled_by_qualified_name(make_registry, plugins_package):
    reg = make_registry(tool_winslow={"enabled_tui_plugins": ["builtin.optin"]})
    reg.discover(plugins_package)
    assert _optin_present(reg)


def test_optin_enabled_by_source(make_registry, install_entrypoint):
    install_entrypoint(dist="acme-plugins")
    reg = make_registry(tool_winslow={"enabled_tui_plugins": ["acme-plugins"]})
    reg.discover_installed()
    assert _optin_present(reg)
