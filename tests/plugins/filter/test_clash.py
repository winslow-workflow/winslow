"""Registration-time failures, surfaced at build (FilterRegistry is
constructed in Workflow.__init__), before any run."""

import pytest

from winslow.exceptions import PluginError

from .conftest import DUP_ONE, DUP_TWO


def test_duplicate_command_raises(list_filters, build_filtered):
    """Two listed filters claiming the same command clash in _do_register."""
    list_filters(DUP_ONE, DUP_TWO)
    with pytest.raises(PluginError, match="already registered"):
        build_filtered()
