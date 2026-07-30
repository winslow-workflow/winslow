"""Custom UI plugins for the discovery/registrability e2e tests. Stands in for
an installed `winslow.tui_plugins` dist (and doubles as a builtin package for the
discover() path). create_widget returns a plain Label so the tests never depend
on real builtin widgets.

Defined in a submodule (not the package __init__) because _iter_package_modules
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


class OptInPlugin(UIPlugin):
    """autoload=False -> absent unless enabled_tui_plugins opts it in."""

    name = "optin"
    slot = Slots.WORKFLOW_LOGS
    label = "Opt In"
    autoload = False

    def create_widget(self, context):
        return Label("optin")
