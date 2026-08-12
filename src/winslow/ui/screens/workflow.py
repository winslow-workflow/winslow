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
from textual.timer import Timer
from textual.css.query import NoMatches


import winslow.ui.builtin_plugins.workflow as workflow_plugins

from winslow.ui.plugin import WorkflowRenderContext, Slots
from winslow.ui.screens.base import SlottedScreen
from winslow.ui.workflow_events import (
    TaskStatusChanged,
    BatchCreated,
    BatchCompleted,
    ExecutionStatusChanged,
    TaskLogUpdated,
    TaskSelected,
)

from winslow.ui.builtin_plugins.workflow.task_list import TaskRow, TaskButton
from winslow.exceptions import SessionEndingError
from winslow.task.info import TaskInfo
from winslow.task.status import TaskStatus, PASSING_STATUSES
from winslow.ui.builtin_plugins.workflow.tasks_pane import TasksPaneWidget

from winslow.ui.modals import TaskDetail, FilterHelp, WorkflowParams
from winslow.ui.filtering import apply_filter_highlight, clear_filter_highlight

from winslow.ui.actions import TaskActionEnum, SESSION_ENDING_MESSAGE
from winslow.ui.store_adapter import StoreEvent


FILTER_PREVIEW_DELAY = 0.5


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


class WorkflowScreen(SlottedScreen):
    PLUGINS_MODULE = workflow_plugins

    BINDINGS = [
        ("ctrl+d", "switch_mode('dashboard')", "Dashboard"),
    ]

    def __init__(self, session):
        self.session = session
        self.workflow = session.workflow
        self._filter_preview_timer: Timer | None = None
        self._filter_matching = None  # tasks matched by the search filter (None = all)
        self._asyncio_tasks: set[asyncio.Task] = set()

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
        return {row.w_task: row for row in self.query(TaskRow).results()}

    async def on_mount(self):
        for task, status in self.workflow.store.items():
            self.propagate_task_status(task, status)

    async def on_screen_resume(self):
        # A session that ended is read-only history. Remove the live Tasks tab, so
        # only the execution History stays. This is idempotent: the tab is gone
        # after the first view of a workflow that ended.
        if self.session.has_ended:
            await self._remove_tasks_tab()
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

    async def _remove_tasks_tab(self):
        try:
            tasks_widget = self.query_one(TasksPaneWidget)
        except NoMatches:
            return
        pane = next((a for a in tasks_widget.ancestors if isinstance(a, TabPane)), None)
        tabbed = next(
            (a for a in tasks_widget.ancestors if isinstance(a, TabbedContent)), None
        )
        if pane is not None and tabbed is not None:
            await tabbed.remove_pane(pane.id)

    def compose(self):
        yield Header()

        context = WorkflowRenderContext(workflow=self.workflow)
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

    def propagate_task_status(self, task, status):
        self.logger.debug(f"Propagate task status: {task} - {status}")
        for slot in (Slots.TASKS_PANE, Slots.TASK_OVERVIEW):
            self._dispatch_to_slot(slot, TaskStatusChanged(task, status))

    def propagate_batch_created(self, batch):
        self._dispatch_to_slot(Slots.TASKS_PANE, BatchCreated(batch))

    def propagate_batch_completed(self, batch):
        self._dispatch_to_slot(Slots.TASKS_PANE, BatchCompleted(batch))

    def propagate_task_log(self, task_uuid, batch_uuid, line):
        batch = self.runner.execution_batches_map.get(batch_uuid)
        if batch:
            self._dispatch_to_slot(
                Slots.TASKS_PANE, TaskLogUpdated(batch, task_uuid, line)
            )

    def propagate_execution_status(self, task_uuid, status, batch_uuid):
        batch = self.runner.execution_batches_map.get(batch_uuid)
        if batch:
            self._dispatch_to_slot(
                Slots.TASKS_PANE, ExecutionStatusChanged(batch, task_uuid, status)
            )

    async def _select_row(self, task_row):
        self.query(TaskRow).remove_class("selected")
        task_row.add_class("selected")

    def show_task_detail(self, task_info):
        self._dispatch_to_slot(Slots.TASK_OVERVIEW, TaskSelected(task_info))

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
        return [row.w_task for row in self.query(TaskRow).results() if row.display]

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

    @refuse_if_ending
    async def _handle_task_run(self, task):
        self._submit_batch(self.runner.submit_run_single, task)

    @refuse_if_ending
    async def _handle_task_check(self, task):
        self._submit_batch(self.runner.submit_check_single, task)

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

    async def _handle_task_info(self, task):
        # The on-demand capture point: the user asked, so the getters evaluate.
        info = TaskInfo.from_task(
            task, evaluate=True, root_dir=self.app.orchestrator.directory
        )
        self.app.push_screen(TaskDetail(info, registry=self.plugin_registry))

    def _clear_filter_preview(self):
        clear_filter_highlight(self.query(TaskRow).results())

    def _apply_filter_preview(self, query):
        self._validate_filter_input(query)
        if not query:
            self._clear_filter_preview()
            return
        try:
            matching = set(
                self.workflow.filter_registry.parse(query).apply(self.workflow.tasks)
            )
        except ValueError:
            self._clear_filter_preview()
            return
        apply_filter_highlight(self.query(TaskRow).results(), matching)

    def _validate_filter_input(self, query):
        self.query_one("#filter-input", Input).validate(query)

    @on(Input.Changed, "#filter-input")
    def handle_filter_changed(self, event):
        if self._filter_preview_timer is not None:
            self._filter_preview_timer.stop()
        query = event.value.strip()
        if not query:
            self._clear_filter_preview()
            self._validate_filter_input(query)
            return
        self._filter_preview_timer = self.set_timer(
            FILTER_PREVIEW_DELAY, lambda: self._apply_filter_preview(query)
        )

    @on(Button.Pressed, ".search-help")
    def handle_filter_help(self, event):
        self.app.push_screen(FilterHelp())

    @on(Button.Pressed, ".workflow-params")
    def handle_workflow_params(self, event):
        self.app.push_screen(WorkflowParams(self.workflow))

    @on(Input.Submitted, "#filter-input")
    def handle_filter(self, event):
        query = event.value.strip()
        if self._filter_preview_timer is not None:
            self._filter_preview_timer.stop()
        self._clear_filter_preview()
        try:
            matching = (
                None
                if not query
                else set(
                    self.workflow.filter_registry.parse(query).apply(
                        self.workflow.tasks
                    )
                )
            )
        except ValueError:
            return
        self._filter_matching = matching
        self._apply_visibility()

    @on(Checkbox.Changed, "#hide-completed")
    @on(Checkbox.Changed, "#hide-skipped")
    def handle_hide_toggle(self, event):
        self._apply_visibility()

    def row_visible(self, task, status):
        """A task row is shown if it passes the search filter and if the 'hide
        completed' and 'hide skipped' toggles do not hide it."""
        if self._filter_matching is not None and task not in self._filter_matching:
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
            row.display = self.row_visible(row.w_task, row.status)

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
        await self._select_row(self.task_rows[event.w_task])

        action_map = {
            TaskActionEnum.RUN: self._handle_task_run,
            TaskActionEnum.CHECK: self._handle_task_check,
            TaskActionEnum.INFO: self._handle_task_info,
        }

        func = action_map[event.action]
        self._fire(func(event.w_task))
