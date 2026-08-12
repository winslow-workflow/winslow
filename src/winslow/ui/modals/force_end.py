from textual import on
from textual.widgets import Button, Label
from textual.containers import Horizontal, VerticalScroll

from .common import BaseModal


class ForceEndModal(BaseModal):
    def __init__(self, session, *args, **kwargs):
        self.session = session
        super().__init__(*args, **kwargs)

    @property
    def modal_title(self):
        n = len(self.session.active_batches)
        return f"Force end - {n} active batch{'es' if n != 1 else ''} still running"

    def on_mount(self):
        # The session can drain by itself while this modal is open. Close the
        # modal, so the user cannot click an old Force end button for batches
        # that already completed.
        self.set_interval(1, self._dismiss_if_drained)

    def _dismiss_if_drained(self):
        if self.session.has_ended or not self.session.active_batches:
            self.dismiss()

    def compose_content(self):
        store_map = self.session.workflow.runner.execution_record_store_map
        with VerticalScroll():
            for batch in self.session.active_batches:
                yield Label(
                    f"{batch.action.value.upper()} · {batch.task_count} task(s)",
                    classes="force-batch",
                )
                store = store_map.get(batch.uuid)
                for record in store.records if store else ():
                    yield Label(f"  {record.info}", classes="force-task")
        with Horizontal(classes="actions"):
            yield Button("Cancel", id="force-cancel")
            yield Button.error("Force end", id="force-confirm")

    @on(Button.Pressed, "#force-confirm")
    def _confirm(self):
        self.session.force_end()
        self.dismiss()

    @on(Button.Pressed, "#force-cancel")
    def _cancel(self):
        self.dismiss()
