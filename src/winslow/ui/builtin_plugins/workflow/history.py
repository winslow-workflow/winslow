import asyncio
import time
from dataclasses import dataclass, replace
from datetime import datetime

from textual import on
from textual.app import ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Label,
    Select,
    TabbedContent,
    TabPane,
)
from textual.containers import Horizontal, Vertical, VerticalScroll

from winslow.actions import StopBatch
from winslow.task.status import PROBLEMATIC_STATUSES, PASSING_STATUSES, TaskStatus
from winslow.ui.css import package_css
from winslow.ui.plugin import UIPlugin, RenderContext, Slots
from winslow.ui.builtin_plugins.workflow.pane_header import PaneSearch
from winslow.ui.builtin_plugins.workflow.tasks_pane import TasksPanePlugin
from winslow.ui.filtering import QuerySearchMixin
from winslow.ui.formatting import format_status_summary
from winslow.ui.modals import TaskDetail
from winslow.ui.modals.common import BaseModal
from winslow.ui.actions import ACTIVE_BATCH_STATUSES
from winslow.ui.icons import get_task_icon
from winslow.ui.reads import port_read
from winslow.ui.widgets.common import TaskRowBase
from winslow.ui.widgets.common.logs import InlineLog
from winslow.ui.widgets.common.params_table import ParamsTable
from winslow.ui.workflow_events import (
    BatchCreated,
    BatchCompleted,
    ExecutionStatusChanged,
    TaskLogUpdated,
)

_CSS = package_css(__package__, "_pane_header.tcss", "history.tcss")

_STATUS_ALL = "all"
_STATUS_OPTIONS = (
    ("all statuses", _STATUS_ALL),
    *((str(status), status) for status in TaskStatus),
)


# (batch option name, pill label, css class). The UI shows a pill on the
# header of the batch card if the flag is set.
_FLAG_PILLS = (
    ("dry_run", "dry run", "dry-run"),
    ("force_run", "force run", "force-run"),
    ("force_success", "force success", "force-success"),
    ("disable_concurrency", "disable concurrency", "disable-concurrency"),
)


@dataclass(frozen=True)
class BatchView:
    """The card model of one batch, from either port shape: a HistoryRow at
    compose time, a BatchInfo from a batch_created event."""

    uuid: str
    action: str
    status: str
    task_count: int
    created_at: float
    options: dict | None
    # (identity key, TaskOutcome or None) per task. An event view starts
    # with no outcomes; the completion refresh fills them (see HistoryPane).
    entries: tuple

    @classmethod
    def from_history_row(cls, row):
        return cls(
            uuid=row.uuid,
            action=row.action,
            status=row.status,
            task_count=row.task_count,
            created_at=row.created_at,
            options=row.options,
            entries=tuple(row.tasks.items()),
        )

    @classmethod
    def from_batch_info(cls, info):
        return cls(
            uuid=info.uuid,
            action=info.action,
            status=info.status,
            task_count=info.task_count,
            created_at=info.created_at,
            options=info.options,
            entries=tuple((key, None) for key in info.tasks),
        )


class RecordRow(TaskRowBase):
    """A history row: w_task holds the roster TaskInfo of the task, and the
    outcome value fills the runtime and the log columns."""

    status = reactive(None, layout=False)

    class Selected(Message):
        def __init__(self, record_row):
            self.record_row = record_row
            super().__init__()

    class InfoRequested(Message):
        def __init__(self, record_row):
            self.record_row = record_row
            super().__init__()

    def __init__(self, batch_uuid, key, info, outcome=None, *args, **kwargs):
        self.batch_uuid = batch_uuid
        self.key = key
        self._outcome = outcome
        # The time of the first status event. The elapsed display of a
        # record without an outcome starts here.
        self._running_since = None
        super().__init__(info, *args, **kwargs)

    @property
    def search_key(self):
        return self.key

    def _fmt_runtime(self):
        outcome = self._outcome
        if outcome is not None and outcome.duration is not None:
            return f"{outcome.duration:.1f}s"
        if outcome is not None and outcome.started_at is not None:
            return f"{time.time() - outcome.started_at:.1f}s"
        if self._running_since is not None:
            return f"{time.time() - self._running_since:.1f}s"
        return ""

    def compose(self) -> ComposeResult:
        info = self.w_task
        index = f"{info.index + 1}." if info is not None else ""
        label = str(info) if info is not None else self.key
        with Horizontal(classes="row-content"):
            yield Label("", classes="icon")
            yield Label(index, classes="index")
            yield Label(label, classes="name")
            yield Label("", classes="runtime")
            yield Label("", classes="status")
            yield InlineLog(classes="log")
        yield Button("info", classes="info-btn")

    def on_click(self, event):
        self.post_message(self.Selected(self))

    def on_mount(self):
        outcome = self._outcome
        if outcome is not None:
            self.status = TaskStatus[outcome.status]
            self.log_line = outcome.last_log
        else:
            self.watch_status(self.status)

    def update_outcome(self, outcome):
        self._outcome = outcome
        self.log_line = outcome.last_log
        # An equal status does not trigger the reactive. Paint directly
        # first, so the runtime column still refreshes.
        self.watch_status(TaskStatus[outcome.status])
        self.status = TaskStatus[outcome.status]

    def watch_status(self, status):
        if not self.is_mounted:
            return
        if self._outcome is None and self._running_since is None:
            self._running_since = time.time()
        self.query_one(".icon", Label).update(get_task_icon(status))
        self.query_one(".runtime", Label).update(self._fmt_runtime())
        self.query_one(".status", Label).update(str(status or ""))

    @on(Button.Pressed, ".info-btn")
    def show_info(self, event):
        event.stop()
        self.post_message(self.InfoRequested(self))


class BatchDetailModal(BaseModal):
    def __init__(self, view, *args, **kwargs):
        self.view = view
        super().__init__(*args, **kwargs)

    @property
    def modal_title(self):
        ts = datetime.fromtimestamp(self.view.created_at).strftime("%H:%M:%S")
        return f"{self.view.action}  ·  {ts}"

    def compose_content(self):
        with TabbedContent():
            with TabPane("Parameters", id="parameters"):
                yield ParamsTable(self.view.options or {})


class BatchCard(Widget):
    def __init__(self, view, infos_by_key, *args, **kwargs):
        self.view = view
        self._infos_by_key = infos_by_key
        super().__init__(*args, **kwargs)

    def refresh_title(self):
        statuses = [row.status for row in self.query(RecordRow).results()]
        completed = sum(1 for s in statuses if s in PASSING_STATUSES)
        problematic = sum(1 for s in statuses if s in PROBLEMATIC_STATUSES)
        summary = format_status_summary(completed, problematic, len(statuses))
        exec_status = self.view.status.replace("_", " ")
        self.border_title = f"{self._title_prefix}  ·  {summary}  ·  {exec_status} "

    def compose(self) -> ComposeResult:
        view = self.view
        ts = datetime.fromtimestamp(view.created_at).strftime("%H:%M:%S")
        short_uuid = str(view.uuid)[:8]
        self._title_prefix = f" {view.action}  ·  {ts}  ·  {short_uuid}"

        options = view.options or {}
        pills = [
            (label, cls) for attr, label, cls in _FLAG_PILLS if options.get(attr)
        ]

        stoppable = view.status in ACTIVE_BATCH_STATUSES
        with Horizontal(classes="batch-header"):
            with Horizontal(classes="batch-tags"):
                for label, cls in pills:
                    yield Label(label, classes=f"tag {cls}")
            yield Button("info", classes="details-btn", variant="default")
            if view.task_count > 1:
                yield Button(
                    "stop",
                    classes="stop-btn",
                    variant="warning",
                    disabled=not stoppable,
                )

        for key, outcome in view.entries:
            yield RecordRow(
                batch_uuid=view.uuid,
                key=key,
                info=self._infos_by_key.get(key),
                outcome=outcome,
            )

    def on_mount(self):
        self.refresh_title()

    def batch_completed(self, info):
        self.view = replace(self.view, status=info.status)
        if self.view.task_count > 1:
            self.query_one(".stop-btn", Button).disabled = True
        self.refresh_title()

    @on(Button.Pressed, ".details-btn")
    def show_details(self):
        self.app.push_screen(BatchDetailModal(self.view))

    @on(Button.Pressed, ".stop-btn")
    def request_stop(self):
        # The workflow screen owns the session client; the card reaches the
        # action handler through it. The acceptance means "stop requested".
        ack = self.screen.submit_action(StopBatch(batch_uuid=self.view.uuid))
        if ack.accepted:
            self.query_one(".stop-btn", Button).disabled = True


class HistoryPane(QuerySearchMixin, Widget):
    DEFAULT_CSS = _CSS

    search_input_id = "record-search"

    def __init__(self, client, infos_by_key, root_dir=None, *args, **kwargs):
        self.client = client
        self._infos_by_key = infos_by_key
        self._root_dir = root_dir
        self._rows: dict[tuple, RecordRow] = {}
        self._cards: dict[str, BatchCard] = {}
        self._init_search()
        self._hide_completed = False
        self._status_filter = _STATUS_ALL
        super().__init__(*args, **kwargs)

    def match_keys(self, query):
        """Match over the record infos through the port: the history scope
        also serves an ended session (see Workflow.filter_keys)."""
        return set(self.client.apply_filter(query, scope="history"))

    def search_rows(self):
        return self.query(RecordRow).results()

    def _row_visible(self, row) -> bool:
        """A row is shown if it passes the search filter, the status filter,
        and the 'hide completed' toggle."""
        if (
            self._filter_matching is not None
            and row.search_key not in self._filter_matching
        ):
            return False
        if self._status_filter != _STATUS_ALL and row.status is not (
            self._status_filter
        ):
            return False
        if self._hide_completed and row.status in PASSING_STATUSES:
            return False
        return True

    def _apply_visibility(self):
        for row in self.query(RecordRow).results():
            row.display = self._row_visible(row)
        for card in self._cards.values():
            card.display = any(row.display for row in card.query(RecordRow).results())

    @on(Input.Changed, "#record-search")
    def handle_search_changed(self, event):
        self.preview_search(event.value)
        if not event.value.strip():
            self._validate_search_input("")

    @on(Input.Submitted, "#record-search")
    def handle_search_submitted(self, event):
        self.submit_search(event.value)

    @on(Checkbox.Changed, "#hide-completed")
    def handle_hide_completed(self, event):
        self._hide_completed = event.value
        self._apply_visibility()

    @on(Select.Changed, "#record-status")
    def handle_status_filter(self, event):
        self._status_filter = event.value
        self._apply_visibility()

    def _register_card(self, card):
        self._cards[card.view.uuid] = card

    def _register_rows(self, widget):
        for row in widget.query(RecordRow):
            self._rows[(row.batch_uuid, row.key)] = row

    def compose(self) -> ComposeResult:
        with Horizontal(id="search-section", classes="pane-header"):
            yield Button("<", classes="mini view-dashboard").with_tooltip(
                "view dashboard"
            )
            yield PaneSearch(placeholder="search records...", input_id="record-search")
            yield Select(
                _STATUS_OPTIONS,
                value=_STATUS_ALL,
                allow_blank=False,
                id="record-status",
            )
            with Horizontal(classes="checkboxes"):
                with Vertical(classes="column"):
                    yield Checkbox("hide completed", id="hide-completed")
                    # The placeholder keeps 'hide completed' on the top row. The
                    # checkbox column of the task bar has the same two rows.
                    yield Checkbox("placeholder", classes="placeholder", disabled=True)
        with VerticalScroll(id="cards-section"):
            rows = port_read(self, self.client.history)
            for row in reversed(rows or ()):
                yield BatchCard(BatchView.from_history_row(row), self._infos_by_key)

    async def on_mount(self):
        for card in self.query(BatchCard):
            self._register_card(card)
        self._register_rows(self)

    def _open_detail(self, row):
        """The RecordDetail of one row, or None with a toast when the record
        is gone or the wire is down."""
        return port_read(self, self.client.record_detail, row.batch_uuid, row.key)

    @on(RecordRow.Selected)
    def on_record_selected(self, event):
        self.query(RecordRow).remove_class("selected")
        event.record_row.add_class("selected")
        # The record info, not the row info: the sweep replaced the stub there.
        detail = self._open_detail(event.record_row)
        if detail is not None:
            self.screen.show_task_detail(detail.info)

    @on(RecordRow.InfoRequested)
    def on_record_info(self, event):
        row = event.record_row
        detail = self._open_detail(row)
        if detail is None:
            return
        self.app.push_screen(
            TaskDetail(
                detail.info,
                registry=self.screen.plugin_registry,
                logs=port_read(self, self.client.log_tail, row.batch_uuid, row.key),
                transient_snapshots=detail.transient_snapshots,
                cache_snapshots=detail.cache_snapshots,
                root_dir=self._root_dir,
            )
        )

    @on(BatchCreated)
    async def on_batch_created(self, event):
        if event.info.uuid in self._cards:
            # A recovery snapshot re-emits known batches; the completed lane
            # refreshes them (see RemoteSessionClient._on_snapshot).
            return
        scroll = self.query_one(VerticalScroll)
        card = BatchCard(BatchView.from_batch_info(event.info), self._infos_by_key)
        await scroll.mount(card, before=0)
        self._register_card(card)
        self._register_rows(card)
        self._apply_visibility()
        card.scroll_visible()

    @on(BatchCompleted)
    def on_batch_completed(self, event):
        card = self._cards.get(event.info.uuid)
        if card:
            card.batch_completed(event.info)
        # The completion refresh reads the exact outcomes: runtime and last
        # log come from the record store, not from a local clock.
        self.run_worker(
            self._refresh_outcomes(event.info.uuid), group="history-refresh"
        )

    async def _refresh_outcomes(self, batch_uuid):
        rows = await asyncio.to_thread(port_read, self, self.client.history)
        if rows is None:
            return
        row = next((r for r in rows if r.uuid == batch_uuid), None)
        if row is None:
            return
        for key, outcome in row.tasks.items():
            if record_row := self._rows.get((batch_uuid, key)):
                record_row.update_outcome(outcome)
        self._apply_visibility()
        if card := self._cards.get(batch_uuid):
            card.refresh_title()

    @on(ExecutionStatusChanged)
    def on_execution_status_changed(self, event):
        row = self._rows.get((event.batch_uuid, event.task_key))
        if row:
            row.status = event.status
            row.display = self._row_visible(row)
        card = self._cards.get(event.batch_uuid)
        if card:
            card.refresh_title()
            card.display = any(r.display for r in card.query(RecordRow).results())

    @on(TaskLogUpdated)
    def on_task_log_updated(self, event):
        row = self._rows.get((event.batch_uuid, event.task_key))
        if row:
            row.log_line = event.line


class HistoryPlugin(UIPlugin):
    slot = Slots.TASKS_PANE
    label = "History"
    priority = TasksPanePlugin.priority + 1

    def create_widget(self, context: RenderContext):
        return HistoryPane(
            client=context.client,
            infos_by_key={info.key: info for info in context.roster},
            root_dir=context.session.root_dir,
        )
