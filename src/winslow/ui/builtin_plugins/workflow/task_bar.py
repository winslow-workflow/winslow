from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Checkbox

from winslow.ui.builtin_plugins.workflow.pane_header import PaneSearch


class TaskBar(Widget):
    def __init__(self, workflow, *args, **kwargs):
        self.workflow = workflow
        super().__init__(*args, **kwargs)

    def compose(self):
        with Horizontal():
            yield Button("<", classes="mini view-dashboard").with_tooltip(
                "view dashboard"
            )
            yield PaneSearch(
                self.workflow, placeholder="filter tasks...", input_id="filter-input"
            )

            with Horizontal(classes="checkboxes"):
                with Vertical(classes="column"):
                    yield Checkbox("hide completed", id="hide-completed")
                    yield Checkbox("hide skipped", id="hide-skipped")
                with Vertical(classes="column"):
                    yield Checkbox(
                        "force run", value=self.workflow.force_run, id="force-run"
                    )
                    yield Checkbox(
                        "force success",
                        value=self.workflow.force_success,
                        id="force-success",
                    )
                with Vertical(classes="column"):
                    yield Checkbox("dry run", value=self.workflow.dry_run, id="dry-run")
                    yield Checkbox(
                        "no concurrency",
                        value=self.workflow.disable_concurrency,
                        id="disable-concurrency",
                    )
            with Horizontal(classes="actions"):
                yield Button("run all", id="run-all", variant="error")
                yield Button("check all", id="check-all", variant="success")
