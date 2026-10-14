"""Custom UI plugins for the registration tests: the built-in path walks the
package, the settings path lists the classes. create_widget returns a plain
Label so the tests never depend on real builtin widgets.

Defined in a submodule (not the package __init__) because register_package
walks submodules only."""

from textual.widgets import Label

from winslow.ui.plugin import Slots, UIPlugin


class AlphaPlugin(UIPlugin):
    name = "alpha"
    slot = Slots.TASK_OVERVIEW
    label = "Alpha"
    priority = 5

    def create_widget(self, context):
        return Label("alpha")


class BetaPlugin(UIPlugin):
    name = "beta"
    slot = Slots.TASK_OVERVIEW
    label = "Beta"
    priority = 3

    def create_widget(self, context):
        return Label("beta")


class NoSlotPlugin(UIPlugin):
    """slot stays None -> _is_candidate is False -> never registers."""

    name = "noslot"

    def create_widget(self, context):
        return Label("noslot")


class LogsPlugin(UIPlugin):
    """A plugin of another slot, for the listing tests."""

    name = "logs"
    slot = Slots.WORKFLOW_LOGS
    label = "Logs"

    def create_widget(self, context):
        return Label("logs")
