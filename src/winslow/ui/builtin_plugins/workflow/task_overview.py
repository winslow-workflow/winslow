from winslow.ui.plugin import UIPlugin, RenderContext, Slots
from winslow.ui.builtin_plugins.workflow.history import HistoryPlugin
from winslow.ui.builtin_plugins.workflow.task_info import TaskInfo
from winslow.ui.builtin_plugins.workflow.tasks_pane import TasksPanePlugin


class TaskOverviewPlugin(UIPlugin):
    slot = Slots.TASK_OVERVIEW
    label = "Task Overview"
    detail_of = (TasksPanePlugin, HistoryPlugin)

    def create_widget(self, context: RenderContext):
        return TaskInfo(store=context.workflow.store)
