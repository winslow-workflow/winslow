import operator
from functools import cached_property

from rich.pretty import Pretty

from textual import on
from textual.reactive import reactive, var
from textual.widget import Widget

from textual.widgets import Label, Rule
from textual.containers import Vertical, VerticalScroll, Horizontal

from winslow.ui.formatting import format_verification
from winslow.ui.widgets.common import TaskStatusIcon
from winslow.ui.workflow_events import TaskSelected, TaskStatusChanged


# This widget does not need to be reactive, because it is always created again.
class TaskInfoRow(Widget):
    def __init__(self, label, value, *args, **kwargs):
        self.label = label
        self.value = value

        super().__init__(*args, **kwargs)

    def compose(self):
        with Vertical():
            yield Label(self.label, classes="attr-name")
            yield Label(self.value, classes="attr-value no-overflow-x")


# (tag label, TaskInfo attribute). The UI shows a pill if the flag is set.
_TAGS = (("premier", "is_premier"), ("terminal", "is_terminal"), ("noop", "is_noop"))


def _task_tags(task_info):
    return [name for name, attr in _TAGS if getattr(task_info, attr, False)]


class TaskTags(Widget):
    """A row of pill labels for the premier, terminal and noop flags of a task."""

    def __init__(self, names, *args, **kwargs):
        self._names = names
        super().__init__(*args, **kwargs)

    def compose(self):
        with Horizontal():
            for name in self._names:
                yield Label(name, classes=f"tag {name}")


class TaskSummary(Widget):
    task_info = reactive(None)

    # key: the label, value: the getter. The getter is called on the task_info
    # object.
    ATTRIBUTE_CONTEXT = {
        "task": "label",
        "groups": "groups_readable",
        "class": "task_class",
        "last verified": lambda task_info: format_verification(
            task_info.checked_at, task_info.effective_ttl
        ),
        "parameters": lambda task_info: (
            Pretty(task_info.parameters) if task_info.parameters else None
        ),
    }

    async def on_mount(self):
        self.border_title = "task information"

    def watch_task_info(self, task_info):
        if task_info is None:
            return

        attr_widget = self.query_one(".task-attributes")
        attr_widget.remove_children(TaskTags)
        attr_widget.remove_children(TaskInfoRow)
        attr_widget.remove_children(Rule)

        rows = []

        for key, value in self.ATTRIBUTE_CONTEXT.items():
            disp_value = "???"

            if value is None:
                disp_value = getattr(task_info, key, None)
            elif isinstance(value, str):
                disp_value = getattr(task_info, value, None)
            elif callable(value):
                disp_value = value(task_info)

            if disp_value is not None:
                rows.append(TaskInfoRow(label=key, value=disp_value))

        tags = _task_tags(task_info)

        for idx, row in enumerate(rows):
            is_last = idx == len(rows) - 1

            attr_widget.mount(row)

            # The pills go directly below the task name, which is the first row.
            if idx == 0 and tags:
                attr_widget.mount(TaskTags(tags))

            if not is_last:
                attr_widget.mount(Rule())

    def compose(self):
        yield Vertical(classes="task-attributes")


class TaskDependencyRow(Widget):
    # A TaskRef: what the row renders. The status arrives by identity key.
    ref = var(None)
    initial_status = var(None)

    def compose(self):
        with Horizontal(classes="dependency-row"):
            icon = TaskStatusIcon(classes="icon")
            icon.status = self.initial_status

            yield icon
            yield Label(self.ref.label, classes="name")


class TaskDependencies(Widget):
    task_info = var(None)
    dependencies = reactive(None)

    def __init__(self, statuses, dependency_type=None, *args, **kwargs):
        self.dependency_type = dependency_type
        # The statuses-by-key mapping that the screen maintains (see
        # WorkflowRenderContext.task_statuses).
        self.statuses = statuses
        super().__init__(*args, **kwargs)

    def compute_dependencies(self):
        if not self.task_info:
            return None
        return self.dependency_getter(self.task_info)

    def watch_dependencies(self, dependencies):
        if not dependencies:
            self.add_class("hidden")
        else:
            self.remove_class("hidden")
            container = self.query_one(".task-dependencies")

            container.remove_children(TaskDependencyRow)

            # None arrives from a context built without task_statuses (the
            # plugin-test seam): the rows then render with an unknown status.
            statuses = self.statuses or {}
            for ref in dependencies:
                dep_row = TaskDependencyRow()
                dep_row.ref = ref
                dep_row.initial_status = statuses.get(ref.key)
                container.mount(dep_row)

    @cached_property
    def dependency_getter(self):
        attr_name = "dependencies"
        if self.dependency_type:
            attr_name = f"{self.dependency_type}_{attr_name}"
        return operator.attrgetter(attr_name)

    async def on_mount(self):
        title = "dependencies"
        if self.dependency_type:
            title = f"{self.dependency_type} {title}"
        self.border_title = title

    def compose(self):
        yield Vertical(classes="task-dependencies")


class TaskInfoPane(Widget):
    """The overview pane of one task. It renders TaskInfo values; the name
    differs from the value class, so an import cannot shadow the model."""

    task_info = reactive(None)

    def __init__(self, statuses, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.statuses = statuses

    @on(TaskSelected)
    def on_task_selected(self, event):
        self.task_info = event.task_info

    @on(TaskStatusChanged)
    def on_task_status_changed(self, event):
        for row in self.query(TaskDependencyRow).results():
            if row.ref is not None and row.ref.key == event.key:
                for icon in row.query(TaskStatusIcon).results():
                    icon.status = event.status

    def watch_task_info(self):
        if not self.task_info:
            return

        self.query("#task-info-placeholder").add_class("hidden")
        self.query(".task-info").remove_class("hidden")

        self.query_one(TaskSummary).task_info = self.task_info
        for dep_widget in self.query(TaskDependencies).results():
            dep_widget.task_info = self.task_info

    def compose(self):

        with Vertical(classes="round centered", id="task-info-placeholder"):
            yield Label("Select a task to see the details.")

        with VerticalScroll(classes="task-info hidden"):
            yield TaskSummary(classes="round")
            yield TaskDependencies(
                statuses=self.statuses, dependency_type="premier", classes="round"
            )
            yield TaskDependencies(statuses=self.statuses, classes="round")
            yield TaskDependencies(
                statuses=self.statuses, dependency_type="terminal", classes="round"
            )
