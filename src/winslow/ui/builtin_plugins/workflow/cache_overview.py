import inspect

from textual import on
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Label, Rule
from textual.containers import Vertical, VerticalScroll

from winslow.ui.plugin import UIPlugin, RenderContext, Slots
from winslow.ui.builtin_plugins.workflow.caches import CachesPanePlugin
from winslow.ui.builtin_plugins.workflow.task_info import TaskInfoRow
from winslow.ui.builtin_plugins.workflow.task_overview import TaskOverviewPlugin
from winslow.ui.workflow_events import CacheSelected


class CacheSummary(Widget):
    # The CacheCard value of the selected cache (see winslow.model).
    card = reactive(None)

    async def on_mount(self):
        self.border_title = "cache information"

    @classmethod
    def _rows(cls, card):
        doc = inspect.cleandoc(card.docstring or "")
        entries = sorted(entry.name for entry in card.entries)
        layers = card.storage.replace(" over ", "\nover ")
        return [
            ("cache", card.name),
            ("scope", card.scope),
            ("description", doc or "(no docstring)"),
            ("storage layers", layers),
            ("entries", "\n".join(entries) or "(none)"),
        ]

    async def watch_card(self, card):
        """Await the removal before the mount: a rapid reselection must not
        interleave the rows of two caches."""
        if card is None:
            return
        container = self.query_one(".cache-attributes")
        await container.remove_children()
        rows = self._rows(card)
        widgets = []
        for index, (label, value) in enumerate(rows):
            widgets.append(TaskInfoRow(label=label, value=value))
            if index != len(rows) - 1:
                widgets.append(Rule())
        await container.mount(*widgets)

    def compose(self):
        yield Vertical(classes="cache-attributes")


class CacheOverview(Widget):
    card = reactive(None)

    @on(CacheSelected)
    def on_cache_selected(self, event):
        self.card = event.card

    def watch_card(self, card):
        if card is None:
            return
        self.query("#cache-overview-placeholder").add_class("hidden")
        self.query(".cache-overview").remove_class("hidden")
        self.query_one(CacheSummary).card = card

    def compose(self):
        with Vertical(classes="round centered", id="cache-overview-placeholder"):
            yield Label("Select a cache to see the details.")
        with VerticalScroll(classes="cache-overview hidden"):
            yield CacheSummary(classes="round")


class CacheOverviewPlugin(UIPlugin):
    slot = Slots.TASK_OVERVIEW
    label = "Cache"
    priority = TaskOverviewPlugin.priority + 1
    detail_of = CachesPanePlugin

    @classmethod
    def should_render(cls, context):
        return bool(context.snapshot.cache_names)

    def create_widget(self, context: RenderContext):
        return CacheOverview()
