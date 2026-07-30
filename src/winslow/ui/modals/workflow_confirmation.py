from textual.widgets import Button
from textual.containers import Horizontal
from textual.message import Message

from winslow.ui.plugin import Slots, WorkflowConfirmationRenderContext

from .common import BaseModal

SLOT = Slots.WORKFLOW_CONFIRMATION


class WorkflowConfirmation(BaseModal):
    CONTENT_CLASSES = "workflow-confirmation-modal"

    class Submitted(Message):
        def __init__(self, workflow_kls, form_values):
            self.workflow_kls = workflow_kls
            self.form_values = form_values
            super().__init__()

    def __init__(self, workflow_kls, form_values, registry, *args, **kwargs):
        self.workflow_kls = workflow_kls
        self.form_values = form_values
        self.registry = registry
        super().__init__(*args, **kwargs)

    @property
    def modal_title(self):
        return f"Start workflow: {self.workflow_kls.get_name()}"

    def compose_content(self):
        context = WorkflowConfirmationRenderContext(
            workflow_kls=self.workflow_kls,
            form_values=self.form_values,
        )
        yield from self.registry.compose_slot(SLOT, context)
        with Horizontal(classes="confirmation-actions"):
            yield Button.success("Proceed", name="workflow-start")
            yield Button.error("Cancel", name="workflow-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.name == "workflow-start":
            self.post_message(
                self.Submitted(
                    workflow_kls=self.workflow_kls,
                    form_values=self.form_values,
                )
            )
        self.dismiss()
