from winslow.ui.plugin import UIPlugin, RenderContext, Slots
from winslow.ui.builtin_plugins.workflow.task_info import TaskInfo


class TaskOverviewPlugin(UIPlugin):
    slot = Slots.TASK_OVERVIEW
    label = "Task Overview"

    def create_widget(self, context: RenderContext):
        return TaskInfo(store=context.workflow.store)
