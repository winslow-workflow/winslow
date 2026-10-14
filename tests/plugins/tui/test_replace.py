"""The replace mechanism, driven through the registry's public register(): a
plugin names the one it evicts, in the same slot."""

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
    replace = "target"

    def create_widget(self, context):
        return Label("replacer")


class ReplacerWrongSlot(UIPlugin):
    name = "replacer-wrong-slot"
    slot = Slots.WORKFLOW_LOGS
    replace = "target"

    def create_widget(self, context):
        return Label("replacer")


def test_replace_evicts_target(make_registry):
    reg = make_registry()
    reg.register(Target)
    reg.register(Replacer)

    assert [p.get_name() for p in reg.for_slot(Slots.TASK_OVERVIEW)] == ["replacer"]


def test_replace_slot_mismatch_raises(make_registry):
    reg = make_registry()
    reg.register(Target)
    with pytest.raises(PluginError, match="slot mismatch"):
        reg.register(ReplacerWrongSlot)


def test_a_replacement_keeps_the_companion_of_its_target(make_registry):
    class Detail(UIPlugin):
        name = "detail"
        slot = Slots.TASK_OVERVIEW
        detail_of = Target

        def create_widget(self, context):
            return Label("detail")

    class Subclassing(Target):
        name = "subclassing"
        replace = "target"

    reg = make_registry()
    reg.register(Target)
    reg.register(Detail)
    reg.register(Subclassing)
    assert reg.companion(Subclassing) is Detail
