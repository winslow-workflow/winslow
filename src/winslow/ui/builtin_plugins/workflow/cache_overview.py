import inspect

from textual import on
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Label, Rule
from textual.containers import Vertical, VerticalScroll

from winslow.cache import declared_entries
from winslow.ui.plugin import UIPlugin, RenderContext, Slots
from winslow.ui.builtin_plugins.workflow.caches import (
    CachesPanePlugin,
    has_registered_caches,
)
from winslow.ui.builtin_plugins.workflow.task_info import TaskInfoRow
from winslow.ui.builtin_plugins.workflow.task_overview import TaskOverviewPlugin
from winslow.ui.workflow_events import CacheSelected


class CacheSummary(Widget):
    cache = reactive(None)

    async def on_mount(self):
        self.border_title = "cache information"

    @classmethod
    def _rows(cls, cache):
        # The raw class __doc__, not inspect.getdoc: the docstring of the
        # framework base must not replace a missing one.
        doc = inspect.cleandoc(type(cache).__doc__ or "")
        entries = sorted(declared_entries(type(cache)))
        layers = cache.describe_storage().replace(" over ", "\nover ")
        return [
            ("cache", cache.get_name()),
            ("scope", cache.scope),
            ("description", doc or "(no docstring)"),
            ("storage layers", layers),
            ("entries", "\n".join(entries) or "(none)"),
        ]

    async def watch_cache(self, cache):
        """Await the removal before the mount: a rapid reselection must not
        interleave the rows of two caches."""
        if cache is None:
            return
        container = self.query_one(".cache-attributes")
        await container.remove_children()
        rows = self._rows(cache)
        widgets = []
        for index, (label, value) in enumerate(rows):
            widgets.append(TaskInfoRow(label=label, value=value))
            if index != len(rows) - 1:
                widgets.append(Rule())
        await container.mount(*widgets)

    def compose(self):
        yield Vertical(classes="cache-attributes")


class CacheOverview(Widget):
    cache = reactive(None)

    @on(CacheSelected)
    def on_cache_selected(self, event):
        self.cache = event.cache

    def watch_cache(self, cache):
        if cache is None:
            return
        self.query("#cache-overview-placeholder").add_class("hidden")
        self.query(".cache-overview").remove_class("hidden")
        self.query_one(CacheSummary).cache = cache

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
        return has_registered_caches(context.workflow)

    def create_widget(self, context: RenderContext):
        return CacheOverview()
