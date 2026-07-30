"""Registration-time failures, both surfaced at build (FilterRegistry is
constructed in Workflow.__init__), before any run."""

import pytest

from winslow.exceptions import PluginError


def test_duplicate_command_raises(install_filters, build_filtered):
    """Two discovered filters claiming the same command clash in _do_register."""
    install_filters(package="plugins.filter.my_filters_clash")
    with pytest.raises(PluginError):
        build_filtered()


def test_enabled_and_disabled_clash_raises(install_filters, build_filtered):
    """The same entry in both enabled_filter_plugins and disabled_filter_plugins is rejected
    by _load_winslow_config."""
    install_filters(
        tool_winslow={
            "enabled_filter_plugins": ["acme-filters.flavor"],
            "disabled_filter_plugins": ["acme-filters.flavor"],
        }
    )
    with pytest.raises(PluginError):
        build_filtered()
