import dataclasses
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
from winslow.filter.builtin import BUILTIN_FILTERS
from winslow.runner.execution import ExecutionStatus
from winslow.task.status import PROBLEMATIC_STATUSES, PASSING_STATUSES, TaskStatus
from winslow.ui.css import package_css
from winslow.ui.plugin import UIPlugin, RenderContext, Slots
from winslow.ui.builtin_plugins.workflow.pane_header import PaneSearch
from winslow.ui.builtin_plugins.workflow.tasks_pane import TasksPanePlugin
from winslow.ui.filtering import SearchFlowMixin
from winslow.ui.formatting import format_status_summary
from winslow.ui.modals import TaskDetail
from winslow.ui.modals.common import BaseModal
from winslow.ui.icons import get_task_icon
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


def _foreign_filter_names(filters):
    """The names of the filters that history cannot run. The test is by exact
    type: a subclass of a builtin filter can touch live-task API."""
    return sorted(
        {type(f).get_name() for f in filters if type(f) not in BUILTIN_FILTERS}
    )


# (execution-context attribute, pill label, css class). The UI shows a pill on
# the header of the batch card if the flag is set.
_FLAG_PILLS = (
    ("dry_run", "dry run", "dry-run"),
    ("force_run", "force run", "force-run"),
    ("force_success", "force success", "force-success"),
    ("disable_concurrency", "disable concurrency", "disable-concurrency"),
)


class RecordRow(TaskRowBase):
    """A history row: w_task holds the TaskInfo of the record, never the task,
    so a row of an ended session retains nothing."""

    status = reactive(None, layout=False)

    class Selected(Message):
        def __init__(self, record_row):
            self.record_row = record_row
            super().__init__()

    def __init__(self, store, info, *args, **kwargs):
        self.exec_store = store
        super().__init__(info, *args, **kwargs)

    def on_click(self, event):
        self.post_message(self.Selected(self))

    @property
    def record(self):
        return self.exec_store.get_record(self.w_task.key)

    @classmethod
    def _fmt_runtime(cls, record):
        if record.duration is not None:
            return f"{record.duration:.1f}s"
        if record.started_at is not None:
            return f"{(datetime.now() - record.started_at).total_seconds():.1f}s"
        return ""

    def compose(self) -> ComposeResult:
        with Horizontal(classes="row-content"):
            yield Label("", classes="icon")
            yield Label(f"{self.w_task.index + 1}.", classes="index")
            yield Label(str(self.w_task), classes="name")
            yield Label("", classes="runtime")
            yield Label("", classes="status")
            yield InlineLog(classes="log")
        yield Button("info", classes="info-btn")

    def on_mount(self):
        self.status = self.exec_store.get(self.w_task.key)
        self.log_line = self.record.last_log

    def watch_status(self, status):
        self.query_one(".icon", Label).update(get_task_icon(status))
        self.query_one(".runtime", Label).update(self._fmt_runtime(self.record))
        self.query_one(".status", Label).update(str(status or ""))

    @on(Button.Pressed, ".info-btn")
    def show_info(self):
        # The record info, not the row info: the sweep replaced the stub there.
        record = self.record
        self.app.push_screen(
            TaskDetail(
                record.info,
                registry=self.screen.plugin_registry,
                logs=record.logs,
                transient_snapshots=record.transient_snapshots,
                cache_snapshots=record.cache_snapshots,
            )
        )


class BatchDetailModal(BaseModal):
    def __init__(self, batch, *args, **kwargs):
        self.batch = batch
        super().__init__(*args, **kwargs)

    @property
    def modal_title(self):
        ts = self.batch.created_at.strftime("%H:%M:%S")
        return f"{self.batch.action.value.upper()}  ·  {ts}"

    def compose_content(self):
        ctx = self.batch.execution_context
        params = {
            f.name: getattr(ctx, f.name)
            for f in dataclasses.fields(ctx)
            if f.name != "batch_uuid"
        }
        with TabbedContent():
            with TabPane("Parameters", id="parameters"):
                yield ParamsTable(params)


class BatchCard(Widget):
    def __init__(self, batch, store, *args, **kwargs):
        self.batch = batch
        self.exec_store = store
        super().__init__(*args, **kwargs)

    def _refresh_title(self):
        statuses = list(self.exec_store.values())
        completed = sum(1 for s in statuses if s in PASSING_STATUSES)
        problematic = sum(1 for s in statuses if s in PROBLEMATIC_STATUSES)
        summary = format_status_summary(completed, problematic, len(statuses))
        exec_status = str(self.batch.status)
        self.border_title = f"{self._title_prefix}  ·  {summary}  ·  {exec_status} "

    def compose(self) -> ComposeResult:
        ts = self.batch.created_at.strftime("%H:%M:%S")
        action = self.batch.action.value.upper()
        short_uuid = str(self.batch.uuid)[:8]
        self._title_prefix = f" {action}  ·  {ts}  ·  {short_uuid}"

        ctx = self.batch.execution_context
        pills = [
            (label, cls)
            for attr, label, cls in _FLAG_PILLS
            if ctx and getattr(ctx, attr, False)
        ]

        stoppable = self.batch.status in (
            ExecutionStatus.QUEUED,
            ExecutionStatus.RUNNING,
        )
        with Horizontal(classes="batch-header"):
            with Horizontal(classes="batch-tags"):
                for label, cls in pills:
                    yield Label(label, classes=f"tag {cls}")
            yield Button("info", classes="details-btn", variant="default")
            if self.batch.task_count > 1:
                yield Button(
                    "stop",
                    classes="stop-btn",
                    variant="warning",
                    disabled=not stoppable,
                )

        for record in self.exec_store.records:
            yield RecordRow(store=self.exec_store, info=record.info)

    def on_mount(self):
        self._refresh_title()

    @on(Button.Pressed, ".details-btn")
    def show_details(self):
        self.app.push_screen(BatchDetailModal(self.batch))

    @on(Button.Pressed, ".stop-btn")
    def request_stop(self):
        # The workflow screen owns the session; the card reaches the action
        # handler through it. The acceptance means "stop requested".
        ack = self.screen.session.actions.submit(StopBatch(batch_uuid=self.batch.uuid))
        if ack.accepted:
            self.query_one(".stop-btn", Button).disabled = True
        else:
            self.notify(ack.reason, severity="warning")


class HistoryPane(SearchFlowMixin, Widget):
    DEFAULT_CSS = _CSS

    def __init__(self, workflow, *args, **kwargs):
        self.workflow = workflow
        self._rows: dict[tuple, RecordRow] = {}
        self._cards: dict[str, BatchCard] = {}
        self._init_search()
        self._filter_matching: set | None = None
        self._hide_completed = False
        self._status_filter = _STATUS_ALL
        super().__init__(*args, **kwargs)

    def _matching_tasks(self, query: str, warn=True) -> set:
        """The row infos that the query matches. Only the builtin filters can
        run on an info; a project filter is refused, with a warning on submit.
        The typing preview passes warn=False, so it does not toast per tick."""
        infos = [row.search_key for row in self.query(RecordRow).results()]
        try:
            parsed = self.workflow.filter_registry.parse(query)
        except ValueError:
            return set()
        foreign = _foreign_filter_names(parsed.filters())
        if foreign:
            if warn:
                self.notify(
                    f"History search supports only the builtin filters "
                    f"(name, group) - not: {', '.join(foreign)}",
                    severity="warning",
                )
            return set()
        return set(parsed.apply(infos))

    def search_rows(self):
        return self.query(RecordRow).results()

    def search_matches(self, query):
        return self._matching_tasks(query, warn=False)

    def apply_search(self, query):
        self._filter_matching = self._matching_tasks(query) if query else None
        self._apply_visibility()

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

    def _get_store(self, batch):
        return self.workflow.runner.execution_record_store_map[batch.uuid]

    def _register_card(self, card):
        self._cards[card.batch.uuid] = card

    def _register_rows(self, widget):
        for row in widget.query(RecordRow):
            self._rows[(row.exec_store.batch_uuid, row.w_task.key)] = row

    def compose(self) -> ComposeResult:
        with Horizontal(id="search-section", classes="pane-header"):
            yield Button("<", classes="mini view-dashboard").with_tooltip(
                "view dashboard"
            )
            yield PaneSearch(
                self.workflow, placeholder="search records...", input_id="record-search"
            )
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
            # Iterate over a list. Iteration over the dict fails if another
            # thread updates the map during the read, because a worker thread
            # registers a batch.
            for batch in reversed(
                list(self.workflow.runner.execution_batches_map.values())
            ):
                yield from self._compose_batch(batch)

    async def on_mount(self):
        for card in self.query(BatchCard):
            self._register_card(card)
        self._register_rows(self)

    def _compose_batch(self, batch):
        yield BatchCard(batch, self._get_store(batch))

    @on(RecordRow.Selected)
    def on_record_selected(self, event):
        self.query(RecordRow).remove_class("selected")
        event.record_row.add_class("selected")
        # The record info, not the row info: the sweep replaced the stub there.
        self.screen.show_task_detail(event.record_row.record.info)

    @on(BatchCreated)
    async def on_batch_created(self, event):
        scroll = self.query_one(VerticalScroll)
        card = BatchCard(event.batch, self._get_store(event.batch))
        await scroll.mount(card, before=0)
        self._register_card(card)
        self._register_rows(card)
        self._apply_visibility()
        card.scroll_visible()

    @on(BatchCompleted)
    def on_batch_completed(self, event):
        card = self._cards.get(event.batch.uuid)
        if not card:
            return
        if card.batch.task_count > 1:
            card.query_one(".stop-btn", Button).disabled = True
        card._refresh_title()

    @on(ExecutionStatusChanged)
    def on_execution_status_changed(self, event):
        row = self._rows.get((event.batch.uuid, event.task_key))
        if row:
            row.status = event.status
            row.display = self._row_visible(row)
        card = self._cards.get(event.batch.uuid)
        if card:
            card._refresh_title()
            card.display = any(r.display for r in card.query(RecordRow).results())

    @on(TaskLogUpdated)
    def on_task_log_updated(self, event):
        row = self._rows.get((event.batch.uuid, event.task_key))
        if row:
            row.log_line = event.line


class HistoryPlugin(UIPlugin):
    slot = Slots.TASKS_PANE
    label = "History"
    priority = TasksPanePlugin.priority + 1

    def create_widget(self, context: RenderContext):
        return HistoryPane(workflow=context.workflow)
