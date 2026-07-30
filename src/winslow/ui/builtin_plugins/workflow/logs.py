from winslow.ui.plugin import UIPlugin, RenderContext, Slots
from winslow.ui.widgets.common import SyncLog


class WorkflowLogsPlugin(UIPlugin):
    slot = Slots.WORKFLOW_LOGS
    label = "Logs"

    def create_widget(self, context: RenderContext):
        return SyncLog(highlight=True, logger=context.workflow.logger)
