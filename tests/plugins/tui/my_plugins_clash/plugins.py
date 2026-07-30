"""Two plugins sharing a name (hence a qualified name) - discovering this
package registers both and forces the name-clash PluginError. Kept out of the
main my_plugins package so it only bites the clash test."""

from textual.widgets import Label

from winslow.ui.plugin import Slots, UIPlugin


class DupA(UIPlugin):
    name = "dup"
    slot = Slots.TASK_OVERVIEW

    def create_widget(self, context):
        return Label("a")


class DupB(UIPlugin):
    name = "dup"
    slot = Slots.TASK_OVERVIEW

    def create_widget(self, context):
        return Label("b")
