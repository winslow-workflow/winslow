import importlib
from types import SimpleNamespace

import pytest

from winslow import autodiscovery
from winslow.ui.plugin import UIPluginRegistry


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
def make_registry(monkeypatch):
    """Build a UIPluginRegistry with an optional [tool.winslow] config. The
    pyproject table is patched before construction because BaseRegistry reads
    the config in __init__ - so a config clash surfaces here, at build."""

    def _make(tool_winslow=None):
        monkeypatch.setattr(
            autodiscovery, "_winslow_pyproject_table", lambda cwd: tool_winslow or {}
        )
        return UIPluginRegistry()

    return _make


@pytest.fixture
def install_entrypoint(monkeypatch):
    """Point the winslow.tui_plugins entry-point group at an importable test
    package, so discover_installed() finds it (source = dist name)."""

    def _install(package="plugins.tui.my_plugins", dist="acme-plugins"):
        entry_point = FakeEntryPoint(importlib.import_module(package), dist)
        monkeypatch.setattr(
            autodiscovery,
            "entry_points",
            lambda group: [entry_point] if group == "winslow.tui_plugins" else [],
        )

    return _install


@pytest.fixture
def plugins_package():
    """The importable test plugin package the discover()/entry-point paths walk."""
    return importlib.import_module("plugins.tui.my_plugins")


@pytest.fixture
def clash_package():
    """A separate package whose two plugins share a name - only the clash test
    discovers it."""
    return importlib.import_module("plugins.tui.my_plugins_clash")
