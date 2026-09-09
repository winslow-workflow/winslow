from textual.containers import Container
from textual.widget import Widget
from textual.widgets import Static

from winslow.ui.plugin import UIPlugin, RenderContext, Slots
from winslow.ui.builtin_plugins.dashboard.workflow_form_widget import (
    WorkflowFormGenerator,
)


class WorkflowFormWidget(Widget):
    DEFAULT_CLASSES = "round"

    def __init__(self, descriptors, **kwargs):
        self._descriptors = descriptors
        super().__init__(**kwargs)

    def compose(self):
        yield Container(
            Static("Choose a workflow to see parameter form."),
            id="workflow-form-placeholder",
            classes="centered",
        )
        for descriptor in self._descriptors.workflows:
            yield from WorkflowFormGenerator(logger=self.app.logger).generate(
                descriptor, self._descriptors.overrides
            )


class DashboardWorkflowFormPlugin(UIPlugin):
    slot = Slots.DASHBOARD_WORKFLOW_FORM
    label = "Workflow Form"

    def create_widget(self, context: RenderContext):
        return WorkflowFormWidget(descriptors=context.descriptors)
