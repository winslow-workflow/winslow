import asyncio
import functools

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

from winslow.ui.filtering import SearchFlowMixin
from winslow.ui.plugin import WorkflowRenderContext, Slots
from winslow.ui.screens.base import SlottedScreen
from winslow.ui.workflow_events import (
    TaskStatusChanged,
    BatchCreated,
    BatchCompleted,
    CacheSelected,
    CacheUpdated,
    ExecutionStatusChanged,
    TaskLogUpdated,
    TaskSelected,
)

from winslow.ui.builtin_plugins.workflow.caches import CachesPane
from winslow.ui.builtin_plugins.workflow.cache_overview import CacheOverviewPlugin
from winslow.ui.builtin_plugins.workflow.task_overview import TaskOverviewPlugin
from winslow.ui.builtin_plugins.workflow.task_list import TaskRow, TaskButton
from winslow.exceptions import SessionEndingError
from winslow.task.status import TaskStatus, PASSING_STATUSES
from winslow.ui.builtin_plugins.workflow.tasks_pane import TasksPaneWidget

from winslow.ui.modals import TaskDetail, FilterHelp, WorkflowParams

from winslow.ui.actions import TaskActionEnum, SESSION_ENDING_MESSAGE
from winslow.ui.store_adapter import StoreEvent


def refuse_if_ending(method):
    """The user-facing part of the batch-admission guard. It refuses the batch
    before the dispatch and shows a toast. Session.batch_admission is the layer
    that enforces the rule, and it refuses each batch that passes this guard (see
    _submit_batch)."""

    @functools.wraps(method)
    async def wrapper(self, *args, **kwargs):
        if self.session.is_ending or self.session.has_ended:
            self.notify(SESSION_ENDING_MESSAGE, severity="warning")
            return
        return await method(self, *args, **kwargs)

    return wrapper


class WorkflowScreen(SearchFlowMixin, SlottedScreen):
    PLUGINS_MODULE = workflow_plugins

    BINDINGS = [
        ("ctrl+d", "switch_mode('dashboard')", "Dashboard"),
    ]

    def __init__(self, session):
        self.session = session
        self.workflow = session.workflow
        self._init_search()
        self._filter_matching = None  # keys matched by the search filter (None = all)
        self._asyncio_tasks: set[asyncio.Task] = set()
        # The statuses-by-key mirror of the live store, read by the DTO-driven
        # panes (see WorkflowRenderContext.task_statuses).
        self.statuses_by_key = {
            task.identity_key: status for task, status in self.workflow.store.items()
        }

        super().__init__()

        # The header, through Screen.title and Screen.sub_title, shows the label
        # for the user. The subtitle holds the technical kebab identifier, which
        # helps to debug when the two names are different, and the identifier
        # options of the instance.
        display_name = self.workflow.get_display_name()
        kebab_name = self.workflow.instance_name
        self.title = display_name
        parts = [kebab_name] if kebab_name != display_name else []
        if self.workflow.identifier_suffix:
            parts.append(self.workflow.identifier_suffix)
        self.sub_title = " | ".join(parts)

    @property
    def logger(self):
        return self.workflow.logger

    @property
    def runner(self):
        return self.workflow.runner

    @property
    def task_rows(self):
        return {row.key: row for row in self.query(TaskRow).results()}

    def _resolve_task(self, key):
        return self.workflow.task_index.resolve(key)

    async def on_mount(self):
        for task, status in self.workflow.store.items():
            self.propagate_task_status(task.identity_key, status)

    async def on_screen_resume(self):
        # A session that ended is read-only history. Remove the live Tasks and
        # Caches tabs, so only the execution History stays. This is idempotent:
        # the tabs are gone after the first view of a workflow that ended.
        if self.session.has_ended:
            await self._remove_live_tabs()
        elif self.session.is_ending:
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

        context = WorkflowRenderContext(
            workflow=self.workflow, task_statuses=self.statuses_by_key
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

    @on(StoreEvent)
    def handle_store_event(self, event):
        event.apply()

    def propagate_task_status(self, key, status):
        self.logger.debug(f"Propagate task status: {key} - {status}")
        self.statuses_by_key[key] = status
        for slot in (Slots.TASKS_PANE, Slots.TASK_OVERVIEW):
            self._dispatch_to_slot(slot, TaskStatusChanged(key, status))

    def propagate_batch_created(self, batch):
        self._dispatch_to_slot(Slots.TASKS_PANE, BatchCreated(batch))

    def propagate_batch_completed(self, batch):
        self._dispatch_to_slot(Slots.TASKS_PANE, BatchCompleted(batch))

    def propagate_task_log(self, task_key, batch_uuid, line):
        batch = self.runner.execution_batches_map.get(batch_uuid)
        if batch:
            self._dispatch_to_slot(
                Slots.TASKS_PANE, TaskLogUpdated(batch, task_key, line)
            )

    def propagate_execution_status(self, task_key, status, batch_uuid):
        batch = self.runner.execution_batches_map.get(batch_uuid)
        if batch:
            self._dispatch_to_slot(
                Slots.TASKS_PANE, ExecutionStatusChanged(batch, task_key, status)
            )

    async def _select_row(self, task_row):
        self.query(TaskRow).remove_class("selected")
        task_row.add_class("selected")

    def show_task_detail(self, task_info):
        self._dispatch_to_slot(Slots.TASK_OVERVIEW, TaskSelected(task_info))
        self.activate_plugin_tab(TaskOverviewPlugin)

    def show_cache_detail(self, cache):
        self._dispatch_to_slot(Slots.TASK_OVERVIEW, CacheSelected(cache))
        self.activate_plugin_tab(CacheOverviewPlugin)

    def propagate_cache_update(self):
        self._dispatch_to_slot(Slots.TASKS_PANE, CacheUpdated())

    @on(TaskRow.Selected)
    async def handle_task_selection(self, event):
        await self._select_row(event.task_row)
        self.show_task_detail(event.task_info)

    def _submit_batch(self, submit, *args):
        # The submit is fast, because it only does the admission and the
        # registration, and the worker of the runner does the work. The submit
        # thus stays on the UI thread, and the typed refusal appears here as a
        # toast.
        try:
            submit(*args)
        except SessionEndingError:
            # refuse_if_ending covers the usual case. This handler catches a batch
            # that the UI dispatched while the session closes. The admission
            # refuses such a batch and does not let it run on a released store.
            self.notify(SESSION_ENDING_MESSAGE, severity="warning")

    def _visible_tasks(self):
        # The rows hold keys; the bulk actions need live tasks for the runner.
        return [
            self._resolve_task(row.key)
            for row in self.query(TaskRow).results()
            if row.display
        ]

    @on(Checkbox.Changed, "#force-run")
    @on(Checkbox.Changed, "#force-success")
    @on(Checkbox.Changed, "#dry-run")
    @on(Checkbox.Changed, "#disable-concurrency")
    def _sync_batch_option(self, event):
        # A live change is safe, because each batch takes a snapshot of these
        # options at its start.
        setattr(
            self.workflow.batch_options, event.control.id.replace("-", "_"), event.value
        )
        # A restore must rebuild the session with the toggles the user set.
        self.workflow.record_batch_options()

    @refuse_if_ending
    async def _handle_task_run(self, key):
        self._submit_batch(self.runner.submit_run_single, self._resolve_task(key))

    @refuse_if_ending
    async def _handle_task_check(self, key):
        self._submit_batch(self.runner.submit_check_single, self._resolve_task(key))

    @refuse_if_ending
    async def _handle_bulk_action(self, verb, submit):
        tasks = self.runner.eligible_tasks(self._visible_tasks())
        if not tasks:
            self.notify(
                "No eligible tasks are selected - cannot process", severity="warning"
            )
            return
        self.notify(f"{verb} {len(tasks)} task{'s' if len(tasks) != 1 else ''}")
        self._submit_batch(submit, tasks)

    async def _handle_bulk_run(self):
        await self._handle_bulk_action("Running", self.runner.submit_run)

    async def _handle_bulk_check(self):
        await self._handle_bulk_action("Checking", self.runner.submit_check)

    @refuse_if_ending
    async def _handle_task_info(self, key):
        task = self._resolve_task(key)
        # The on-demand capture point: the user asked, so the getters evaluate.
        info = self.workflow.task_info(
            task,
            evaluate=True,
            root_dir=self.app.orchestrator.directory,
        )
        # log_key travels beside the info: the routing key is process-local,
        # and the TaskInfo value stays wire-portable.
        self.app.push_screen(
            TaskDetail(info, registry=self.plugin_registry, log_key=task.log_key)
        )

    def search_rows(self):
        return self.query(TaskRow).results()

    def _matching_keys(self, query):
        """Run the filter on the live tasks and project the match set to
        identity keys, the shape that the rows test."""
        matched = self.workflow.filter_registry.parse(query).apply(self.workflow.tasks)
        return {task.identity_key for task in matched}

    def search_matches(self, query):
        # None marks an unparseable query: the preview clears (see
        # SearchFlowMixin._preview_now).
        self._validate_filter_input(query)
        try:
            return self._matching_keys(query)
        except ValueError:
            return None

    def apply_search(self, query):
        try:
            matching = None if not query else self._matching_keys(query)
        except ValueError:
            return
        self._filter_matching = matching
        self._apply_visibility()

    def _validate_filter_input(self, query):
        self.query_one("#filter-input", Input).validate(query)

    @on(Input.Changed, "#filter-input")
    def handle_filter_changed(self, event):
        self.preview_search(event.value)
        if not event.value.strip():
            self._validate_filter_input("")

    @on(Button.Pressed, ".search-help")
    def handle_filter_help(self, event):
        self.app.push_screen(FilterHelp())

    @on(Button.Pressed, ".workflow-params")
    def handle_workflow_params(self, event):
        self.app.push_screen(WorkflowParams(self.workflow))

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
