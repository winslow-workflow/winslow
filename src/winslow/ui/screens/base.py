from itertools import cycle

from textual import on
from textual.screen import Screen
from textual.containers import Horizontal
from textual.widgets import TabbedContent, TabPane

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
        force_tabbed = self.plugin_registry.any_tabbed(context, *slots)
        with Horizontal(classes=pane_class):
            for slot in slots:
                yield from self.plugin_registry.compose_slot(
                    slot, context, force_tabbed=force_tabbed
                )

    @on(TabbedContent.TabActivated)
    def _activate_companion_tab(self, event):
        """The switch of a master tab brings its companion detail tab forward
        (see detail_of). A tab with no companion changes nothing, so the rule
        cannot loop."""
        master = getattr(event.tabbed_content.active_pane, "w_plugin", None)
        if master is None:
            return
        if detail := self.plugin_registry.companion(master):
            self.activate_plugin_tab(detail)

    def activate_plugin_tab(self, plugin_cls):
        """Bring the tab of the plugin to the front, so a selection or a
        master switch is visible at once. Without a tab this is a no-op."""
        for pane in self.query(TabPane).results():
            stamped = getattr(pane, "w_plugin", None)
            if stamped is not None and issubclass(stamped, plugin_cls):
                tabbed = next(
                    (a for a in pane.ancestors if isinstance(a, TabbedContent)), None
                )
                if tabbed is not None:
                    tabbed.active = pane.id
                return

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
