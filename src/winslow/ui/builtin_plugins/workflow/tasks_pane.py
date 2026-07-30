from textual import on
from textual.widget import Widget

from winslow.ui.css import package_css
from winslow.ui.plugin import UIPlugin, RenderContext, Slots
from winslow.ui.builtin_plugins.workflow.task_bar import TaskBar
from winslow.ui.builtin_plugins.workflow.task_list import TaskList, TaskRow
from winslow.task.status import PASSING_STATUSES
from winslow.ui.workflow_events import TaskStatusChanged, TaskLogUpdated


class TasksPaneWidget(Widget):
    DEFAULT_CSS = package_css(__package__, "_pane_header.tcss", "tasks_pane.tcss")

    def __init__(self, workflow, *args, **kwargs):
        self.workflow = workflow
        self._row_map: dict = {}
        super().__init__(*args, **kwargs)

    def on_mount(self):
        self._row_map = {row.w_task: row for row in self.query(TaskRow).results()}

    @on(TaskStatusChanged)
    def on_task_status_changed(self, event):
        if row := self._row_map.get(event.task):
            row.status = event.status
            if event.status in PASSING_STATUSES:
                row.log_line = ""
            row.display = self.screen.row_visible(event.task, event.status)

    @on(TaskLogUpdated)
    def on_task_log_updated(self, event):
        if row := self._row_map.get(event.task):
            row.log_line = event.line

    def compose(self):
        yield TaskBar(workflow=self.workflow, classes="task-bar round pane-header")
        yield TaskList(workflow=self.workflow, classes="round")


class TasksPanePlugin(UIPlugin):
    slot = Slots.TASKS_PANE
    label = "Tasks"
    priority = 5

    def create_widget(self, context: RenderContext):
        return TasksPaneWidget(workflow=context.workflow)
