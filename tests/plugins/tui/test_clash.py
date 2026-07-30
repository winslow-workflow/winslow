"""Registration-time failures: a duplicate qualified name at discovery, and a
config that both enables and disables the same entry."""

import pytest

from winslow.exceptions import PluginError


def test_duplicate_name_raises(make_registry, clash_package):
    reg = make_registry()
    with pytest.raises(PluginError):
        reg.discover(clash_package)


def test_enabled_and_disabled_clash_raises(make_registry):
    with pytest.raises(PluginError):
        make_registry(
            tool_winslow={
                "enabled_tui_plugins": ["builtin.alpha"],
                "disabled_tui_plugins": ["builtin.alpha"],
            }
        )
