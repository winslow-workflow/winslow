from textual import on
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static, Label, Button
from textual.containers import VerticalScroll, Horizontal
from textual.message import Message

from winslow.task.status import (
    TaskStatus,
    RUNNABLE_STATUSES,
    CHECKABLE_STATUSES,
)

from winslow.ui.widgets.common import (
    TaskStatusWidget,
    TaskStatusIcon,
    TaskStatusLabel,
    TaskRowBase,
    InlineLog,
)

from winslow.ui.actions import TaskActionEnum, SESSION_ENDING_MESSAGE
from winslow.exceptions import MisconfigurationError


# The UI hides the action buttons, and does not disable them, for a status that a
# task cannot leave during a session.
NO_ACTION_STATUSES = frozenset((TaskStatus.SKIPPED,))


class TaskButton(Button):
    ACTION = None
    LABEL = None
    VARIANT = None
    ENABLED_STATUSES = None
    HIDDEN_STATUSES = None
    # True if this button starts a batch. Such a button locks while the session
    # drains.
    BLOCKS_ON_ENDING = False

    status = reactive(None)
    session_ending = reactive(False)

    class TaskAction(Message):
        """The payload is the identity key; the screen resolves it through
        the task index (see docs/ui-plugins.md)."""

        def __init__(self, key, action):
            self.key = key
            self.action = action
            super().__init__()

    def __init__(self, key, action, *args, **kwargs):
        super().__init__(label=self.LABEL, variant=self.VARIANT)

        self.key = key
        self.action = action

    async def on_click(self):
        payload = self.TaskAction(key=self.key, action=self.ACTION)
        self.post_message(payload)

    def watch_status(self, status):
        if self.HIDDEN_STATUSES:
            self.display = status not in self.HIDDEN_STATUSES
        self._refresh_disabled()

    def watch_session_ending(self, ending):
        self._refresh_disabled()

    def _refresh_disabled(self):
        # A status update still arrives while the drain runs, so the lock of the
        # ending session has priority over the status at each recompute.
        if self.BLOCKS_ON_ENDING and self.session_ending:
            self.disabled = True
            self.tooltip = SESSION_ENDING_MESSAGE
            return
        if not self.ENABLED_STATUSES:
            self.disabled = False
        else:
            self.disabled = self.status not in self.ENABLED_STATUSES


class RunButton(TaskButton):
    LABEL = "run"
    VARIANT = "error"
    ENABLED_STATUSES = RUNNABLE_STATUSES
    HIDDEN_STATUSES = NO_ACTION_STATUSES
    ACTION = TaskActionEnum.RUN
    BLOCKS_ON_ENDING = True


class CheckButton(TaskButton):
    LABEL = "check"
    VARIANT = "success"
    ENABLED_STATUSES = CHECKABLE_STATUSES
    HIDDEN_STATUSES = NO_ACTION_STATUSES
    ACTION = TaskActionEnum.CHECK
    BLOCKS_ON_ENDING = True


class InfoButton(TaskButton):
    LABEL = "info"
    VARIANT = "default"
    ACTION = TaskActionEnum.INFO


class TaskButtons(Widget):
    def __init__(self, info, *args, **kwargs):
        self.w_info = info
        super().__init__(*args, **kwargs)

    @property
    def key(self):
        # Derived from the info, so the buttons cannot act on a task other
        # than the one the row shows.
        return self.w_info.key

    def compose(self):
        with Horizontal(classes="actions"):
            if not self.w_info.is_noop:
                yield RunButton(key=self.key, action="run")
            yield CheckButton(key=self.key, action="check")
            yield InfoButton(key=self.key, action="info")


class TaskRow(TaskRowBase):
    """A live task row: it holds the identity key and a non-evaluated
    TaskInfo (compare RecordRow)."""

    status = reactive(None)

    class Selected(Message):
        def __init__(self, task_row):
            self.task_row = task_row
            super().__init__()

        @property
        def task_info(self):
            return self.task_row.w_task

    def __init__(self, info, *args, **kwargs):
        super().__init__(info, *args, **kwargs)

        if info.groups:
            self.border_title = info.groups_readable

    @property
    def key(self):
        return self.w_task.key

    @property
    def search_key(self):
        return self.key

    def watch_status(self, status):
        for widget in self.query(TaskStatusWidget).results():
            widget.status = status
        for button in self.query(TaskButton).results():
            button.status = status

    def on_mount(self):
        # The watcher runs during compose, before the children exist. Apply
        # the value again now, because the children are mounted.
        self.watch_status(self.status)

    async def on_click(self, event):
        self.post_message(self.Selected(task_row=self))

    def compose(self):
        info = self.w_task

        with Horizontal(classes="row-content"):
            yield TaskStatusIcon(classes="icon")
            yield Static(f"{info.index + 1}.", classes="index")
            yield Label(info.label, classes="name")
            yield TaskStatusLabel(classes="status")
            yield InlineLog(classes="log")
        yield TaskButtons(info=info)


class TaskList(VerticalScroll):
    def __init__(self, workflow, *args, **kwargs):
        self.workflow = workflow
        super().__init__(*args, **kwargs)

    @on(TaskRow.Selected)
    async def handle_task_selection(self, event):
        self.query(TaskRow).remove_class("selected")
        event.task_row.add_class("selected")

    def compose(self):
        # If the --filter at launch is bad, the UI shows each task and does not
        # stop. get_filtered_tasks raises an error for an invalid filter.
        try:
            tasks = self.workflow.get_filtered_tasks()
        except MisconfigurationError:
            tasks = self.workflow.tasks

        for task in tasks:
            row = TaskRow(info=self.workflow.task_info(task))
            row.status = self.workflow.store[task]
            yield row
