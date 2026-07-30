from itertools import cycle

from textual.screen import Screen
from textual.containers import Horizontal

from winslow.ui.plugin import UIPluginRegistry
from winslow.ui.slot_inspector import COLORS, SlotHighlight, iter_slots


class SlottedScreen(Screen):
    """A screen that fills its panes from the plugin slots. A subclass sets
    PLUGINS_MODULE, where the built-in plugins are found and the installed
    plugins are added, and it puts the panes in place with _compose_slots. The
    shared shell style in common.tcss selects this type."""

    PLUGINS_MODULE = None

    BINDINGS = [("ctrl+g", "toggle_slot_inspector", "Slots")]

    def __init__(self, *args, **kwargs):
        self.plugin_registry = UIPluginRegistry()
        self.plugin_registry.discover(self.PLUGINS_MODULE)
        self.plugin_registry.discover_installed()
        super().__init__(*args, **kwargs)

    def _compose_slots(self, pane_class, slots, context):
        # One tabbed slot makes the whole row tabbed, so the row keeps one
        # visual line.
        force_tabbed = self.plugin_registry.any_tabbed(*slots)
        with Horizontal(classes=pane_class):
            for slot in slots:
                yield from self.plugin_registry.compose_slot(
                    slot, context, force_tabbed=force_tabbed
                )

    def action_toggle_slot_inspector(self):
        """Cover each slot with a colored overlay that names it. The same key
        removes the covers. This shows a plugin author what each slot spans."""
        covers = self.query(SlotHighlight)
        if covers:
            covers.remove()
            return
        colors = cycle(COLORS)
        for slot in iter_slots():
            containers = self.query(f".{slot.id}")
            if not containers:
                continue
            color = next(colors)
            for container in containers:
                container.mount(SlotHighlight(slot.id, color))
