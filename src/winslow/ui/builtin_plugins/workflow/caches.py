import asyncio

from enum import StrEnum

from textual import on
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Button, Input, Label, Select
from textual.containers import Horizontal, VerticalScroll

from winslow.actions import ClearCacheEntries, LoadCacheEntries
from winslow.cache import GLOBAL_SCOPE, WORKFLOW_SCOPE
from winslow.model import EntryState
from winslow.ui.css import package_css
from winslow.ui.filtering import SearchFlowMixin
from winslow.ui.plugin import UIPlugin, RenderContext, Slots
from winslow.ui.builtin_plugins.workflow.history import HistoryPlugin
from winslow.ui.modals.cache_value import CacheValue
from winslow.ui.widgets.common import TaskRowBase
from winslow.ui.widgets.common.logs import InlineLog
from winslow.ui.workflow_events import CacheUpdated, SessionEnded

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


class CacheEntryRow(TaskRowBase):
    """One entry of a cache card, in the shared task-row shell. The value
    preview uses the log column, in the style of an inline task log."""

    # always_update: show_unobservable rewrites the row outside the reactive,
    # so an unchanged state after a recovery must still repaint.
    state = reactive(None, layout=False, always_update=True)
    error = reactive(None, layout=False, always_update=True)

    class Action(Message):
        def __init__(self, cache_name, entry_name, action):
            self.cache_name = cache_name
            self.entry_name = entry_name
            self.action = action
            super().__init__()

    class Selected(Message):
        def __init__(self, row):
            self.row = row
            super().__init__()

    def __init__(self, cache_name, entry_name, *args, **kwargs):
        self.cache_name = cache_name
        self.entry_name = entry_name
        # The row key fills w_task: the filter helpers match on it, and it
        # stays unique across same-named entries of different caches.
        super().__init__((cache_name, entry_name), *args, **kwargs)

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
        instead of a stale state (see CacheCard.unobservable)."""
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
        self.post_message(self.Action(self.cache_name, self.entry_name, action))


class CacheCardWidget(Widget):
    class Selected(Message):
        def __init__(self, cache_name):
            self.cache_name = cache_name
            super().__init__()

    def __init__(self, card, *args, **kwargs):
        # The CacheCard value at compose time; the pane refreshes the rows
        # from later snapshots (see CachesPane._apply).
        self.card = card
        self._title_prefix = f" {card.name}  ·  {card.scope}"
        super().__init__(*args, **kwargs)

    @property
    def cache_name(self):
        return self.card.name

    def compose(self):
        for entry in self.card.entries:
            yield CacheEntryRow(self.card.name, entry.name)

    def on_mount(self):
        self.border_title = f"{self._title_prefix} "

    def on_click(self, event):
        self.post_message(self.Selected(self.card.name))

    def refresh_summary(self, infos):
        warm = sum(1 for info in infos if info.state is EntryState.WARM)
        self.border_title = f"{self._title_prefix}  ·  {warm}/{len(infos)} warm "


class CachesPane(SearchFlowMixin, Widget):
    """The caches of the session, rendered from the caches read of the port.
    The cards mount on the first snapshot; every later snapshot updates the
    rows in place."""

    DEFAULT_CSS = _CSS

    def __init__(self, client, session_ended=False, *args, **kwargs):
        self.client = client
        self._session_ended = session_ended
        self._tick_timer = None
        self._rows = {}
        self._cards = {}
        # The latest CacheCard per name, for the overview pane selection.
        self._card_values = {}
        self._search = ""
        self._init_search()
        self._scope = _SCOPE_ALL
        # True while a snapshot thread runs: exclusive workers cancel only the
        # awaiting coroutine, so the flag stops the threads from stacking.
        self._collecting = False
        super().__init__(*args, **kwargs)

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
        yield VerticalScroll(id="cache-cards")

    def on_mount(self):
        # A pane of an ended session is a static snapshot: no tick, and the
        # caches read of the port has nothing live to observe.
        if not self._session_ended:
            self._tick_timer = self.set_interval(
                CACHE_TICK_SECONDS, self._schedule_refresh
            )
            self._schedule_refresh()

    @on(SessionEnded)
    def _stop_ticking(self):
        if self._tick_timer is not None:
            self._tick_timer.stop()

    # --- refresh: every render comes from a caches read -----------------------

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
            cards = await asyncio.to_thread(self.client.caches)
            await self._apply(cards)
        except Exception:
            # A raise would tear the whole app down: log and continue.
            self.app.logger.error("The cache pane repaint failed.", exc_info=True)
        finally:
            self._collecting = False

    async def _mount_card(self, card):
        widget = CacheCardWidget(card)
        await self.query_one("#cache-cards", VerticalScroll).mount(widget)
        self._cards[card.name] = widget
        for row in widget.query(CacheEntryRow).results():
            self._rows[(card.name, row.entry_name)] = row

    async def _apply(self, cards):
        for card in cards:
            self._card_values[card.name] = card
            widget = self._cards.get(card.name)
            if widget is None:
                await self._mount_card(card)
                widget = self._cards[card.name]
            if card.error is not None:
                widget.border_subtitle = " storage error "
                for row in widget.query(CacheEntryRow).results():
                    row.tooltip = card.error
                    row.show_unobservable()
                continue
            widget.border_subtitle = ""
            widget.refresh_summary(card.info)
            for info in card.info:
                if row := self._rows.get((card.name, info.entry_name)):
                    row.tooltip = None
                    row.state = info.state
                    row.error = info.error
                    row.log_line = card.values.get(info.entry_name) or ""
        self._apply_visibility()

    # --- filters ----------------------------------------------------------

    def _row_scope(self, row):
        card = self._card_values.get(row.cache_name)
        return card.scope if card is not None else None

    def _row_visible(self, row):
        if self._scope != _SCOPE_ALL and self._row_scope(row) != self._scope:
            return False
        if not self._search:
            return True
        return (
            self._search in row.entry_name.lower()
            or self._search in row.cache_name.lower()
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
            row.search_key
            for row in self._rows.values()
            if query in row.entry_name.lower() or query in row.cache_name.lower()
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

    def _select_cache(self, cache_name):
        if card := self._card_values.get(cache_name):
            self.screen.show_cache_detail(card)

    @on(CacheCardWidget.Selected)
    def on_card_selected(self, event):
        self._select_cache(event.cache_name)

    @on(CacheEntryRow.Selected)
    def on_row_selected(self, event):
        self.query(CacheEntryRow).remove_class("selected")
        event.row.add_class("selected")
        self._select_cache(event.row.cache_name)

    # --- actions ------------------------------------------------------------

    @on(CacheEntryRow.Action)
    def handle_entry_action(self, event):
        pair = (event.cache_name, event.entry_name)
        match event.action:
            case CacheAction.VIEW:
                self.app.push_screen(
                    CacheValue.for_entry(self.client, *pair)
                )
            case CacheAction.LOAD:
                self.screen.submit_action(LoadCacheEntries(entries=(pair,)))
            case CacheAction.CLEAR:
                self.screen.submit_action(ClearCacheEntries(entries=(pair,)))

    def _visible_pairs(self):
        return tuple(
            (row.cache_name, row.entry_name)
            for row in self._rows.values()
            if self._row_visible(row)
        )

    @on(Button.Pressed, "#cache-load-all")
    def handle_load_all(self):
        pairs = self._visible_pairs()
        if not pairs:
            self.notify("No cache entries are visible - nothing to load")
            return
        ack = self.screen.submit_action(LoadCacheEntries(entries=pairs))
        if ack.accepted:
            self.notify(
                f"Loading {len(pairs)} cache entr{'y' if len(pairs) == 1 else 'ies'}"
            )

    @on(Button.Pressed, "#cache-clear-all")
    def handle_clear_all(self):
        pairs = self._visible_pairs()
        if not pairs:
            self.notify("No cache entries are visible - nothing to clear")
            return
        ack = self.screen.submit_action(ClearCacheEntries(entries=pairs))
        if ack.accepted:
            self.notify(
                f"Clearing {len(pairs)} cache entr{'y' if len(pairs) == 1 else 'ies'}"
            )


class CachesPanePlugin(UIPlugin):
    slot = Slots.TASKS_PANE
    label = "Caches"
    priority = HistoryPlugin.priority + 1

    @classmethod
    def should_render(cls, context):
        return bool(context.snapshot.cache_names)

    def create_widget(self, context: RenderContext):
        return CachesPane(
            client=context.client,
            session_ended=context.snapshot.status == "ENDED",
        )
