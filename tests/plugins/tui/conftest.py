import importlib

import pytest

from winslow.conf import settings
from winslow.ui.plugin import UIPluginRegistry

ALPHA = "plugins.tui.my_plugins.plugins.AlphaPlugin"
BETA = "plugins.tui.my_plugins.plugins.BetaPlugin"
LOGS = "plugins.tui.my_plugins.plugins.LogsPlugin"


@pytest.fixture
def make_registry():
    return UIPluginRegistry


@pytest.fixture
def list_plugins(monkeypatch):
    """Set TUI_PLUGINS to the given paths, the way a project module does."""

    def _list(*paths):
        monkeypatch.setattr(settings, "TUI_PLUGINS", list(paths))

    return _list


@pytest.fixture
def plugins_package():
    """The importable test plugin package the built-in path walks."""
    return importlib.import_module("plugins.tui.my_plugins")


@pytest.fixture
def clash_package():
    """A separate package whose two plugins share a name - only the clash test
    registers it."""
    return importlib.import_module("plugins.tui.my_plugins_clash")
