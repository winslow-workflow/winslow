import asyncio
from functools import partial, wraps

from textual import on
from textual.widgets import (
    Footer,
    Header,
    Input,
    Button,
    Checkbox,
    TabbedContent,
    TabPane,
)
from textual.css.query import NoMatches


import winslow.ui.builtin_plugins.workflow as workflow_plugins

from winslow.ui.filtering import QuerySearchMixin
from winslow.ui.plugin import WorkflowRenderContext, Slots
from winslow.ui.screens.base import SlottedScreen
from winslow.ui.workflow_events import (
    TaskStatusChanged,
    BatchCreated,
    BatchCompleted,
    CacheSelected,
    CacheUpdated,
    ExecutionStatusChanged,
    SessionEnded,
    TaskLogUpdated,
    TaskSelected,
)

from winslow.actions import CheckTasks, RunTasks
from winslow.events import (
    BatchCompletedEvent,
    BatchCreatedEvent,
    ExecutionStatusEvent,
    LogLineEvent,
    SessionEndedEvent,
    TaskStatusEvent,
)
from winslow.model import CacheUpdatedEvent
from winslow.ui.builtin_plugins.workflow.caches import CachesPane
from winslow.ui.builtin_plugins.workflow.cache_overview import CacheOverviewPlugin
from winslow.ui.builtin_plugins.workflow.task_overview import TaskOverviewPlugin
from winslow.ui.builtin_plugins.workflow.task_list import TaskRow, TaskButton
from winslow.task.status import TaskStatus, PASSING_STATUSES
from winslow.ui.builtin_plugins.workflow.tasks_pane import TasksPaneWidget

from winslow.ui.modals import TaskDetail, FilterHelp, WorkflowParams

from winslow.ui.actions import TaskActionEnum, SESSION_ENDING_MESSAGE
from winslow.ui.store_adapter import StoreEvent


def refuse_if_ending(method):
    """The guard of the direct reads that bypass the action handler, today
    only the task detail. A batch action needs no guard: its refused ack
    carries the same message (see ActionHandler)."""

    @wraps(method)
    async def wrapper(self, *args, **kwargs):
        if self.session_status in ("ENDING", "ENDED"):
            self.notify(SESSION_ENDING_MESSAGE, severity="warning")
            return
        return await method(self, *args, **kwargs)

    return wrapper


class WorkflowScreen(QuerySearchMixin, SlottedScreen):
    """One session, rendered from the session port alone: the reads and the
    actions go through the SessionClient, the live updates arrive as port
    subscriptions (see winslow.client)."""

    PLUGINS_MODULE = workflow_plugins

    BINDINGS = [
        ("ctrl+d", "switch_mode('dashboard')", "Dashboard"),
    ]

    search_input_id = "filter-input"

    def __init__(self, client, session_row):
        self.client = client
        self.session_row = session_row
        self.session_id = session_row.session_id
        self._init_search()
        self._asyncio_tasks: set[asyncio.Task] = set()
        # {batch uuid: toast verb} of the bulk submits that wait for their
        # batch_created event, which carries the admitted task count.
        self._pending_bulk = {}
        # The handler per subscribed topic, for the teardown paths.
        self._port_handlers = {}

        self.snapshot = client.snapshot()
        self.session_status = self.snapshot.status
        self.roster = client.roster()
        # The toggles of this client: view state, seeded from the session
        # baseline and sent with every submit (see RunTasks.options).
        self.batch_options = dict(client.batch_options())
        # The statuses-by-key mirror of the session, read by the DTO-driven
        # panes (see WorkflowRenderContext.task_statuses).
        self.statuses_by_key = {
            key: TaskStatus[name] for key, name in self.snapshot.tasks.items()
        }

        super().__init__()

        # The header, through Screen.title and Screen.sub_title, shows the label
        # for the user. The subtitle holds the technical kebab identifier, which
        # helps to debug when the two names are different, and the identifier
        # options of the instance.
        display_name = session_row.display_name
        kebab_name = session_row.instance_name
        self.title = display_name
        parts = [kebab_name] if kebab_name != display_name else []
        if session_row.identifier_suffix:
            parts.append(session_row.identifier_suffix)
        self.sub_title = " | ".join(parts)

    @property
    def logger(self):
        return self.app.logger

    @property
    def task_rows(self):
        return {row.key: row for row in self.query(TaskRow).results()}

    # --- port subscriptions ---------------------------------------------------

    def connect(self):
        """Wire the screen onto the session events of the port, once, right
        after the install. The bus close at session end disconnects every
        lane except the cache lane (see _on_session_ended)."""
        for topic, method in (
            (TaskStatusEvent, self._on_task_status),
            (ExecutionStatusEvent, self._on_execution_status),
            (BatchCreatedEvent, self._on_batch_created),
            (BatchCompletedEvent, self._on_batch_completed),
            (LogLineEvent, self._on_log_line),
            (SessionEndedEvent, self._on_session_ended),
            (CacheUpdatedEvent, self._on_cache_updated),
        ):
            handler = self._relay(method)
            self._port_handlers[topic] = handler
            self.client.subscribe(topic, handler)

    def _relay(self, method):
        """A port handler that moves the event to the UI thread. The publish
        thread returns immediately (see StoreEvent)."""

        def handler(event):
            self.post_message(StoreEvent(partial(method, event)))

        return handler

    def _disconnect_topic(self, topic):
        handler = self._port_handlers.pop(topic, None)
        if handler is not None:
            self.client.unsubscribe(topic, handler)

    def prepare_session_end(self):
        """Detach the cache lane before the end submits: the cache release of
        the session end then repaints nothing (see Session._finalize_end)."""
        self._disconnect_topic(CacheUpdatedEvent)

    @on(StoreEvent)
    def handle_store_event(self, event):
        event.apply()

    def _on_task_status(self, event):
        self.propagate_task_status(event.key, event.status)

    def _on_execution_status(self, event):
        self._dispatch_to_slot(
            Slots.TASKS_PANE,
            ExecutionStatusChanged(event.batch_uuid, event.task_key, event.status),
        )

    def _on_batch_created(self, event):
        if verb := self._pending_bulk.pop(event.info.uuid, None):
            count = event.info.task_count
            self.notify(f"{verb} {count} task{'s' if count != 1 else ''}")
        self._dispatch_to_slot(Slots.TASKS_PANE, BatchCreated(event.info))

    def _on_batch_completed(self, event):
        self._dispatch_to_slot(Slots.TASKS_PANE, BatchCompleted(event.info))

    def _on_log_line(self, event):
        self._dispatch_to_slot(
            Slots.TASKS_PANE,
            TaskLogUpdated(event.batch_uuid, event.task_key, event.line),
        )

    def _on_session_ended(self, event):
        self.session_status = "ENDED"
        self._disconnect_topic(CacheUpdatedEvent)
        self._dispatch_to_slot(Slots.TASKS_PANE, SessionEnded())

    def _on_cache_updated(self, event):
        self._dispatch_to_slot(Slots.TASKS_PANE, CacheUpdated())

    # --- lifecycle --------------------------------------------------------------

    def _refresh_from_snapshot(self):
        """Overlay the current snapshot: a port event that arrived before the
        screen ran is healed here. The mirror only updates, because the store
        of an ended session is empty."""
        snapshot = self.client.snapshot()
        self.session_status = snapshot.status
        for key, name in snapshot.tasks.items():
            self.propagate_task_status(key, TaskStatus[name])

    async def on_mount(self):
        self._refresh_from_snapshot()

    async def on_screen_resume(self):
        self._refresh_from_snapshot()
        # A session that ended is read-only history. Remove the live Tasks and
        # Caches tabs, so only the execution History stays. This is idempotent:
        # the tabs are gone after the first view of a workflow that ended.
        if self.session_status == "ENDED":
            await self._remove_live_tabs()
        elif self.session_status == "ENDING":
            # The end starts from the dashboard, so the user can enter this screen
            # again only with a resume. A lock here thus covers each path.
            self._disable_batch_controls()

    def _disable_batch_controls(self):
        for button in self.query(TaskButton).results():
            button.session_ending = True
        for selector in ("#run-all", "#check-all"):
            for button in self.query(selector).results(Button):
                button.disabled = True
                button.tooltip = SESSION_ENDING_MESSAGE

    async def _remove_live_tabs(self):
        for widget_type in (TasksPaneWidget, CachesPane):
            try:
                widget = self.query_one(widget_type)
            except NoMatches:
                continue
            pane = next((a for a in widget.ancestors if isinstance(a, TabPane)), None)
            tabbed = next(
                (a for a in widget.ancestors if isinstance(a, TabbedContent)), None
            )
            if pane is not None and tabbed is not None:
                await tabbed.remove_pane(pane.id)

    def compose(self):
        yield Header()

        # The first view can come long after the install: a fresh snapshot
        # composes the panes from the current state.
        self.snapshot = self.client.snapshot()
        context = WorkflowRenderContext(
            client=self.client,
            session=self.session_row,
            snapshot=self.snapshot,
            roster=self.roster,
            task_statuses=self.statuses_by_key,
        )
        yield from self._compose_slots(
            "top-pane", (Slots.TASKS_PANE, Slots.TASK_OVERVIEW), context
        )
        yield from self._compose_slots(
            "bottom-pane", (Slots.WORKFLOW_LOGS, Slots.WORKFLOW_RESOURCES), context
        )

        yield Footer()

    def _dispatch_to_slot(self, slot, event):
        for widget in self.query(f".{slot.id}-content").results():
            widget.post_message(event)

    def propagate_task_status(self, key, status):
        self.statuses_by_key[key] = status
        for slot in (Slots.TASKS_PANE, Slots.TASK_OVERVIEW):
            self._dispatch_to_slot(slot, TaskStatusChanged(key, status))

    async def _select_row(self, task_row):
        self.query(TaskRow).remove_class("selected")
        task_row.add_class("selected")

    def show_task_detail(self, task_info):
        self._dispatch_to_slot(Slots.TASK_OVERVIEW, TaskSelected(task_info))
        self.activate_plugin_tab(TaskOverviewPlugin)

    def show_cache_detail(self, card):
        self._dispatch_to_slot(Slots.TASK_OVERVIEW, CacheSelected(card))
        self.activate_plugin_tab(CacheOverviewPlugin)

    @on(TaskRow.Selected)
    async def handle_task_selection(self, event):
        await self._select_row(event.task_row)
        self.show_task_detail(event.task_info)

    def submit_action(self, action):
        # The submit is fast, because the handler only does the admission and
        # the registration, and the worker of the runner does the work. The
        # submit thus stays on the UI thread, and a refusal appears here as a
        # toast (see ActionHandler).
        ack = self.client.submit(action)
        if not ack.accepted:
            self.notify(ack.reason, severity="warning")
        return ack

    def _visible_keys(self):
        return tuple(row.key for row in self.query(TaskRow).results() if row.display)

    @on(Checkbox.Changed, "#force-run")
    @on(Checkbox.Changed, "#force-success")
    @on(Checkbox.Changed, "#dry-run")
    @on(Checkbox.Changed, "#disable-concurrency")
    def _sync_batch_option(self, event):
        # The toggle is view state of this client alone; the next submit
        # carries it (see RunTasks.options).
        name = event.control.id.replace("-", "_")
        self.batch_options[name] = event.value

    def _submit_batch(self, action_class, keys):
        return self.submit_action(
            action_class(keys=keys, options=dict(self.batch_options))
        )

    async def _handle_task_run(self, key):
        self._submit_batch(RunTasks, (key,))

    async def _handle_task_check(self, key):
        self._submit_batch(CheckTasks, (key,))

    async def _handle_bulk_action(self, verb, action_class):
        ack = self._submit_batch(action_class, self._visible_keys())
        if not ack.accepted:
            return
        # The batch_created event carries the admitted count. The toast waits
        # for it (see _on_batch_created).
        self._pending_bulk[ack.batch_uuid] = verb

    async def _handle_bulk_run(self):
        await self._handle_bulk_action("Running", RunTasks)

    async def _handle_bulk_check(self):
        await self._handle_bulk_action("Checking", CheckTasks)

    @refuse_if_ending
    async def _handle_task_info(self, key):
        # The on-demand capture point: the user asked, so the getters evaluate.
        info = self.client.task_detail(key)
        self.app.push_screen(
            TaskDetail(
                info,
                registry=self.plugin_registry,
                client=self.client,
                task_key=key,
            )
        )

    def search_rows(self):
        return self.query(TaskRow).results()

    def match_keys(self, query):
        """Run the filter through the port: project filter code lives
        server-side (see Workflow.filter_keys)."""
        return set(self.client.apply_filter(query))

    @on(Input.Changed, "#filter-input")
    def handle_filter_changed(self, event):
        self.preview_search(event.value)
        if not event.value.strip():
            self._validate_search_input("")

    @on(Button.Pressed, ".search-help")
    def handle_filter_help(self, event):
        self.app.push_screen(FilterHelp())

    @on(Button.Pressed, ".workflow-params")
    def handle_workflow_params(self, event):
        self.app.push_screen(
            WorkflowParams(
                self.session_row.instance_name, self.client.session_params()
            )
        )

    @on(Input.Submitted, "#filter-input")
    def handle_filter(self, event):
        self.submit_search(event.value)

    @on(Checkbox.Changed, "#hide-completed")
    @on(Checkbox.Changed, "#hide-skipped")
    def handle_hide_toggle(self, event):
        self._apply_visibility()

    def row_visible(self, key, status):
        """A task row is shown if it passes the search filter and if the 'hide
        completed' and 'hide skipped' toggles do not hide it."""
        if self._filter_matching is not None and key not in self._filter_matching:
            return False
        if (
            self.query_one("#hide-completed", Checkbox).value
            and status in PASSING_STATUSES
        ):
            return False
        if (
            self.query_one("#hide-skipped", Checkbox).value
            and status is TaskStatus.SKIPPED
        ):
            return False
        return True

    def _apply_visibility(self):
        for row in self.query(TaskRow).results():
            row.display = self.row_visible(row.search_key, row.status)

    def _fire(self, coro):
        task = asyncio.create_task(coro)
        self._asyncio_tasks.add(task)
        task.add_done_callback(self._asyncio_tasks.discard)
        task.add_done_callback(self._handle_task_error)

    def _handle_task_error(self, asyncio_task):
        if not asyncio_task.cancelled() and asyncio_task.exception():
            self.logger.error(
                "Unhandled error in background task", exc_info=asyncio_task.exception()
            )

    @on(Button.Pressed, "#run-all")
    def handle_run_all(self):
        self._fire(self._handle_bulk_run())

    @on(Button.Pressed, "#check-all")
    def handle_check_all(self):
        self._fire(self._handle_bulk_check())

    @on(TaskButton.TaskAction)
    async def handle_task_action(self, event):
        await self._select_row(self.task_rows[event.key])

        action_map = {
            TaskActionEnum.RUN: self._handle_task_run,
            TaskActionEnum.CHECK: self._handle_task_check,
            TaskActionEnum.INFO: self._handle_task_info,
        }

        func = action_map[event.action]
        self._fire(func(event.key))
