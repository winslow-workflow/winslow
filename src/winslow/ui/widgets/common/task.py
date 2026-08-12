from textual.reactive import reactive
from textual.widget import Widget

from winslow.task import TaskStatus
from winslow.ui.css import package_css
from winslow.ui.icons import get_task_icon

from .logs import InlineLog


class TaskStatusWidget(Widget):
    # task is optional: a dependency row has none, its status arrives by uuid.
    status = reactive(TaskStatus.INITIALIZED)

    def __init__(self, task=None, *args, **kwargs):
        self.w_task = task
        super().__init__(*args, **kwargs)


class TaskStatusIcon(TaskStatusWidget):
    def render(self):
        return get_task_icon(self.status)


class TaskStatusLabel(TaskStatusWidget):
    def render(self):
        return f"{self.status}"


class TaskRowBase(Widget):
    """The shared shell of a task row, in the live task list and in the execution
    history. It is a horizontal strip of .row-content columns with the actions at
    the end, and task_row.tcss holds its style. It is also the row contract that
    filtering.py uses: the .w_task attribute with add_class and remove_class. A
    subclass fills the columns and controls the meaning of its statuses."""

    DEFAULT_CSS = package_css(__package__, "task_row.tcss")

    log_line = reactive("", layout=False)

    def __init__(self, task, *args, **kwargs):
        # The name is w_task, to prevent a clash with the task names of Textual
        # and of asyncio.
        self.w_task = task
        super().__init__(*args, **kwargs)

    def watch_log_line(self, line):
        self.query_one(InlineLog).content = line
