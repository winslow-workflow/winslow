from textual.widget import Widget

from winslow.ui.plugin import UIPlugin, RenderContext, Slots
from winslow.ui.widgets.common import FilterableOptionList


class WorkflowSelectorWidget(Widget):
    def __init__(self, names, **kwargs):
        self._names = names
        super().__init__(**kwargs)

    def compose(self):
        yield FilterableOptionList(
            *self._names, title="Workflows", id="workflow-selector"
        )


class DashboardWorkflowsPlugin(UIPlugin):
    slot = Slots.DASHBOARD_WORKFLOWS
    label = "Workflows"

    def create_widget(self, context: RenderContext):
        names = [d.workflow for d in context.descriptors.workflows]
        return WorkflowSelectorWidget(names)
