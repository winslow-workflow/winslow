"""The replace mechanism, driven through the registry's public register(): a
plugin can evict another in the same slot, but only coherently (matching slot)
and only if it will actually load."""

import pytest
from textual.widgets import Label

from winslow.exceptions import PluginError
from winslow.ui.plugin import Slots, UIPlugin


class Target(UIPlugin):
    name = "target"
    slot = Slots.TASK_OVERVIEW

    def create_widget(self, context):
        return Label("target")


class Replacer(UIPlugin):
    name = "replacer"
    slot = Slots.TASK_OVERVIEW
    replace = "builtin.target"

    def create_widget(self, context):
        return Label("replacer")


class ReplacerWrongSlot(UIPlugin):
    name = "replacer-wrong-slot"
    slot = Slots.WORKFLOW_LOGS
    replace = "builtin.target"

    def create_widget(self, context):
        return Label("replacer")


class OptInReplacer(UIPlugin):
    name = "optin-replacer"
    slot = Slots.TASK_OVERVIEW
    replace = "builtin.target"
    autoload = False

    def create_widget(self, context):
        return Label("replacer")


def test_replace_evicts_target(make_registry):
    reg = make_registry()
    reg.register(Target, source="builtin")
    reg.register(Replacer, source="builtin")

    assert [p.get_name() for p in reg.for_slot(Slots.TASK_OVERVIEW)] == ["replacer"]


def test_replace_slot_mismatch_raises(make_registry):
    reg = make_registry()
    reg.register(Target, source="builtin")
    with pytest.raises(PluginError):
        reg.register(ReplacerWrongSlot, source="builtin")


def test_replace_with_optin_not_enabled_raises(make_registry):
    reg = make_registry()
    # replace + autoload=False + not enabled would evict the target with nothing
    # replacing it - rejected up front in UIPluginRegistry._register.
    with pytest.raises(PluginError):
        reg.register(OptInReplacer, source="builtin")
