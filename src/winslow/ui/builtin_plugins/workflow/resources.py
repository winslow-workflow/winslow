from winslow.ui.plugin import UIPlugin, RenderContext, Slots
from winslow.ui.builtin_plugins.common.system_resources import SystemStats


class WorkflowSystemResourcesPlugin(UIPlugin):
    slot = Slots.WORKFLOW_RESOURCES
    label = "System Resources"

    def create_widget(self, context: RenderContext):
        return SystemStats()
