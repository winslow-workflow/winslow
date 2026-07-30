from textual.widget import Widget
from textual.widgets import Label

from winslow.ui.plugin import UIPlugin, WorkflowConfirmationRenderContext, Slots
from winslow.ui.widgets.common import ParamsTable


class WorkflowConfirmationWidget(Widget):
    def __init__(self, form_values, *args, **kwargs):
        self.form_values = form_values
        super().__init__(*args, **kwargs)

    def compose(self):
        yield Label("Workflow", classes="section-label")
        yield ParamsTable(self.form_values.workflow, classes="params")
        yield Label("Orchestrator", classes="section-label")
        yield ParamsTable(self.form_values.orchestrator, classes="params")


class WorkflowConfirmationPlugin(UIPlugin):
    slot = Slots.WORKFLOW_CONFIRMATION
    label = "Parameters"

    def create_widget(self, context: WorkflowConfirmationRenderContext):
        return WorkflowConfirmationWidget(context.form_values)
