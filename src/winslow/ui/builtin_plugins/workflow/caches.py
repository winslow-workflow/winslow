import asyncio

from contextlib import nullcontext
from enum import StrEnum
from functools import partial

from textual import on
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Button, Input, Label, Select
from textual.containers import Horizontal, VerticalScroll

from winslow.cache import (
    GLOBAL_SCOPE,
    WORKFLOW_SCOPE,
    BaseCache,
    EntryState,
    StorageRecord,
    declared_entries,
)
from winslow.ui.css import package_css
from winslow.ui.filtering import SearchFlowMixin
from winslow.ui.plugin import UIPlugin, RenderContext, Slots
from winslow.ui.builtin_plugins.workflow.history import HistoryPlugin
from winslow.ui.modals.cache_value import CacheValue
from winslow.ui.widgets.common import TaskRowBase
from winslow.ui.widgets.common.logs import InlineLog
from winslow.ui.workflow_events import CacheUpdated
from winslow.util import execute_in_threads, safe_repr

_CSS = package_css(__package__, "_pane_header.tcss", "caches.tcss")

# A cache event triggers an immediate refresh, so the tick only covers the
# lazy ttl expiry.
CACHE_TICK_SECONDS = 2.0

_SCOPE_ALL = "all"
_SCOPE_OPTIONS = tuple(
    (scope, scope) for scope in (_SCOPE_ALL, WORKFLOW_SCOPE, GLOBAL_SCOPE)
)


class CacheAction(StrEnum):
    """One row action. The button name carries it (see CacheEntryRow)."""

    CLEAR = "clear"
    LOAD = "load"
    VIEW = "view"


# (action, button variant), in display order.
_ENTRY_ACTIONS = (
    (CacheAction.CLEAR, "error"),
    (CacheAction.LOAD, "success"),
    (CacheAction.VIEW, "default"),
)


def has_registered_caches(workflow):
    """With no cache in either scope, the cache panes stay off the screen."""
    return bool(workflow.workflow_cache.caches() or workflow.global_cache.caches())


class CacheEntryRow(TaskRowBase):
    """One entry of a cache card, in the shared task-row shell. The value
    preview uses the log column, in the style of an inline task log."""

    # always_update: show_unobservable rewrites the row outside the reactive,
    # so an unchanged state after a recovery must still repaint.
    state = reactive(None, layout=False, always_update=True)
    error = reactive(None, layout=False, always_update=True)

    class Action(Message):
        def __init__(self, cache, entry_name, action):
            self.cache = cache
            self.entry_name = entry_name
            self.action = action
            super().__init__()

    class Selected(Message):
        def __init__(self, row):
            self.row = row
            super().__init__()

    def __init__(self, cache, entry_name, *args, **kwargs):
        self.cache = cache
        self.entry_name = entry_name
        # The row key fills w_task: the filter helpers match on it, and it
        # stays unique across same-named entries of different caches.
        super().__init__((cache, entry_name), *args, **kwargs)

    def compose(self):
        with Horizontal(classes="row-content"):
            yield Label(self.entry_name, classes="name")
            yield Label("", classes="status")
            yield InlineLog(classes="log")
        with Horizontal(classes="actions"):
            for action, variant in _ENTRY_ACTIONS:
                yield Button(
                    action, name=action, classes=f"{action}-btn", variant=variant
                )

    def on_mount(self):
        self.watch_state(self.state)

    def on_click(self, event):
        # Stop the bubble: the card click would select the cache a second time.
        event.stop()
        self.post_message(self.Selected(self))

    def watch_state(self, state):
        if not self.is_mounted or state is None:
            return
        self.remove_class("unobservable", *(str(s) for s in EntryState))
        self.add_class(str(state))
        self.query_one(".status", Label).update(str(state))

    def show_unobservable(self):
        """The storage of the cache cannot be observed: the row shows that
        instead of a stale state (see CachesPane._collect)."""
        if not self.is_mounted:
            return
        self.remove_class(*(str(s) for s in EntryState))
        self.add_class("unobservable")
        self.query_one(".status", Label).update("storage error")

    def watch_error(self, error):
        if self.is_mounted:
            label = self.query_one(".status", Label)
            label.tooltip = f"{error.origin}: {error.message}" if error else None

    @on(Button.Pressed)
    def post_action(self, event):
        event.stop()
        action = CacheAction(event.button.name)
        self.post_message(self.Action(self.cache, self.entry_name, action))


class CacheCard(Widget):
    class Selected(Message):
        def __init__(self, cache):
            self.cache = cache
            super().__init__()

    def __init__(self, cache, *args, **kwargs):
        self.cache = cache
        self._title_prefix = f" {cache.get_name()}  ·  {cache.scope}"
        super().__init__(*args, **kwargs)

    def compose(self):
        # From the declarations, never from a peek: a broken storage must not
        # break the compose (see CachesPane._collect).
        for name in declared_entries(type(self.cache)):
            yield CacheEntryRow(self.cache, name)

    def on_mount(self):
        self.border_title = f"{self._title_prefix} "

    def on_click(self, event):
        self.post_message(self.Selected(self.cache))

    def refresh_summary(self, infos):
        warm = sum(1 for info in infos if info.state is EntryState.WARM)
        self.border_title = f"{self._title_prefix}  ·  {warm}/{len(infos)} warm "


class CachesPane(SearchFlowMixin, Widget):
    DEFAULT_CSS = _CSS

    def __init__(self, workflow, *args, **kwargs):
        self.workflow = workflow
        self._rows = {}
        self._cards = {}
        self._search = ""
        self._init_search()
        self._scope = _SCOPE_ALL
        # The caches the tick cannot observe: the first failure logs, a
        # recovery clears the mark and the badge.
        self._unobservable = set()
        # True while a collect thread runs: exclusive workers cancel only the
        # awaiting coroutine, so the flag stops the threads from stacking.
        self._collecting = False
        super().__init__(*args, **kwargs)

    def _caches(self):
        return (
            *self.workflow.workflow_cache.caches(),
            *self.workflow.global_cache.caches(),
        )

    def compose(self):
        with Horizontal(id="cache-header", classes="pane-header"):
            yield Button("<", classes="mini view-dashboard").with_tooltip(
                "view dashboard"
            )
            with Horizontal(classes="search"):
                yield Input(placeholder="search entries...", id="cache-search")
            yield Select(
                _SCOPE_OPTIONS, value=_SCOPE_ALL, allow_blank=False, id="cache-scope"
            )
            with Horizontal(classes="actions"):
                yield Button("clear all", id="cache-clear-all", variant="error")
                yield Button("load all", id="cache-load-all", variant="success")
        with VerticalScroll(id="cache-cards"):
            for cache in self._caches():
                yield CacheCard(cache)

    def on_mount(self):
        for card in self.query(CacheCard).results():
            self._cards[card.cache] = card
            for row in card.query(CacheEntryRow).results():
                self._rows[(row.cache, row.entry_name)] = row
        self.set_interval(CACHE_TICK_SECONDS, self._schedule_refresh)
        self._schedule_refresh()

    # --- refresh: every render comes from a peek --------------------------

    def _schedule_refresh(self):
        # exclusive: a new refresh replaces a slow one instead of queueing.
        self.run_worker(self._refresh(), exclusive=True, group="cache-refresh")

    @on(CacheUpdated)
    def on_cache_updated(self, event):
        self._schedule_refresh()

    async def _refresh(self):
        if self._collecting:
            return
        self._collecting = True
        try:
            snapshot = await asyncio.to_thread(self._collect)
        finally:
            self._collecting = False
        try:
            self._apply(snapshot)
        except Exception:
            # A raise would tear the whole app down: log and continue.
            self.workflow.logger.error("The cache pane repaint failed.", exc_info=True)

    def _collect(self):
        """One peek pass over every cache, off the UI thread. A cache whose
        storage raises is marked unobservable; the others keep refreshing."""
        snapshot = []
        for cache in self._caches():
            try:
                infos = cache.inspect()
                values = {
                    info.entry_name: self._value_repr(cache, info)
                    for info in infos
                    if info.written_at is not None
                }
            except Exception:
                if cache not in self._unobservable:
                    self._unobservable.add(cache)
                    self.workflow.logger.error(
                        f"The cache pane cannot observe '{cache.get_name()}'.",
                        exc_info=True,
                    )
                snapshot.append((cache, None, None))
                continue
            self._unobservable.discard(cache)
            snapshot.append((cache, infos, values))
        return snapshot

    @classmethod
    def _value_repr(cls, cache, info):
        record = cache.peek(info.entry_name)
        return safe_repr(record.value) if isinstance(record, StorageRecord) else ""

    def _apply(self, snapshot):
        for cache, infos, values in snapshot:
            card = self._cards.get(cache)
            if infos is None:
                if card:
                    card.border_subtitle = " storage error "
                    for row in card.query(CacheEntryRow).results():
                        row.show_unobservable()
                continue
            if card:
                card.border_subtitle = ""
                card.refresh_summary(infos)
            for info in infos:
                if row := self._rows.get((cache, info.entry_name)):
                    row.state = info.state
                    row.error = info.error
                    row.log_line = values.get(info.entry_name, "")

    # --- filters ----------------------------------------------------------

    def _row_visible(self, row):
        if self._scope != _SCOPE_ALL and row.cache.scope != self._scope:
            return False
        if not self._search:
            return True
        return (
            self._search in row.entry_name.lower()
            or self._search in row.cache.get_name().lower()
        )

    def _apply_visibility(self):
        for row in self._rows.values():
            row.display = self._row_visible(row)
        for card in self._cards.values():
            card.display = any(
                row.display for row in card.query(CacheEntryRow).results()
            )

    def search_rows(self):
        return self._rows.values()

    def search_matches(self, query):
        """The row keys whose entry name or cache name contains the query."""
        query = query.lower()
        return {
            row.w_task
            for row in self._rows.values()
            if query in row.entry_name.lower() or query in row.cache.get_name().lower()
        }

    def apply_search(self, query):
        self._search = query.lower()
        self._apply_visibility()

    @on(Input.Changed, "#cache-search")
    def handle_search_changed(self, event):
        self.preview_search(event.value)

    @on(Input.Submitted, "#cache-search")
    def handle_search_submitted(self, event):
        self.submit_search(event.value)

    @on(Select.Changed, "#cache-scope")
    def handle_scope(self, event):
        self._scope = event.value
        self._apply_visibility()

    # --- selection ----------------------------------------------------------

    def _select_cache(self, cache):
        self.screen.show_cache_detail(cache)

    @on(CacheCard.Selected)
    def on_card_selected(self, event):
        self._select_cache(event.cache)

    @on(CacheEntryRow.Selected)
    def on_row_selected(self, event):
        self.query(CacheEntryRow).remove_class("selected")
        event.row.add_class("selected")
        self._select_cache(event.row.cache)

    # --- actions ------------------------------------------------------------

    def _run_action(self, work):
        """One cache action off the UI thread, then a repaint from a re-peek.
        The session log scope routes the cache emissions to the session logger,
        so the log pane shows them (see Session.log_scope)."""
        session = self.workflow.session
        scope = session.log_scope() if session else nullcontext()

        async def action():
            with scope:
                try:
                    await asyncio.to_thread(work)
                except Exception:
                    # A raise would tear the whole app down: log and continue.
                    self.workflow.logger.error(
                        "The cache action failed.", exc_info=True
                    )
            self._schedule_refresh()

        self.run_worker(action(), group="cache-actions")

    def _load_entry(self, cache, entry_name):
        """Load one entry and continue past a failure: the log carries the
        error, and the repaint shows the row state."""
        try:
            getattr(cache, entry_name)
        except Exception:
            self.workflow.logger.error(
                f"Cache '{cache.get_name()}': the load of '{entry_name}' failed.",
                exc_info=True,
            )

    @on(CacheEntryRow.Action)
    def handle_entry_action(self, event):
        match event.action:
            case CacheAction.VIEW:
                self.app.push_screen(
                    CacheValue.for_entry(
                        event.cache, event.entry_name, self.workflow.logger
                    )
                )
            case CacheAction.LOAD:
                self._run_action(
                    partial(self._load_entry, event.cache, event.entry_name)
                )
            case CacheAction.CLEAR:
                self._run_action(partial(event.cache.invalidate, event.entry_name))

    def _visible_rows(self):
        return [row for row in self._rows.values() if self._row_visible(row)]

    @on(Button.Pressed, "#cache-load-all")
    def handle_load_all(self):
        jobs = [(row.cache, row.entry_name) for row in self._visible_rows()]
        if not jobs:
            self.notify("No cache entries are visible - nothing to load")
            return
        self.notify(f"Loading {len(jobs)} cache entr{'y' if len(jobs) == 1 else 'ies'}")
        # One flat pool, like the eager population: independent entries load
        # in parallel, a dependent blocks on the field lock of its upstream.
        self._run_action(partial(execute_in_threads, self._load_entry, jobs))

    @on(Button.Pressed, "#cache-clear-all")
    def handle_clear_all(self):
        caches = sorted({row.cache for row in self._visible_rows()}, key=str)
        if not caches:
            self.notify("No caches are visible - nothing to clear")
            return
        self.notify(f"Clearing {len(caches)} cache{'' if len(caches) == 1 else 's'}")
        self._run_action(partial(execute_in_threads, BaseCache.invalidate_all, caches))


class CachesPanePlugin(UIPlugin):
    slot = Slots.TASKS_PANE
    label = "Caches"
    priority = HistoryPlugin.priority + 1

    @classmethod
    def should_render(cls, context):
        return has_registered_caches(context.workflow)

    def create_widget(self, context: RenderContext):
        return CachesPane(workflow=context.workflow)
