from textual.containers import Container
from textual.widget import Widget
from textual.widgets import Static

from winslow.ui.plugin import UIPlugin, RenderContext, Slots
from winslow.ui.builtin_plugins.dashboard.workflow_form_widget import (
    WorkflowFormGenerator,
)


class WorkflowFormWidget(Widget):
    DEFAULT_CLASSES = "round"

    def __init__(self, workflow_context, orchestrator, **kwargs):
        self._workflow_context = workflow_context
        self._orchestrator = orchestrator
        super().__init__(**kwargs)

    def compose(self):
        yield Container(
            Static("Choose a workflow to see parameter form."),
            id="workflow-form-placeholder",
            classes="centered",
        )
        for workflow_kls, workflow_config in self._workflow_context.items():
            yield from WorkflowFormGenerator(logger=self._orchestrator.logger).generate(
                workflow_kls=workflow_kls,
                orchestrator_config_meta=self._orchestrator.config_meta,
                orchestrator_config=self._orchestrator.orchestrator_config,
                workflow_config_meta=workflow_kls.config_meta,
                workflow_config=workflow_config,
            )


class DashboardWorkflowFormPlugin(UIPlugin):
    slot = Slots.DASHBOARD_WORKFLOW_FORM
    label = "Workflow Form"

    def create_widget(self, context: RenderContext):
        return WorkflowFormWidget(
            workflow_context=context.workflow_context,
            orchestrator=context.orchestrator,
        )
