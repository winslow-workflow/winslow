from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Checkbox

from winslow.ui.builtin_plugins.workflow.pane_header import PaneSearch


class TaskBar(Widget):
    """The header controls of the Tasks pane. The checkboxes start from the
    live batch option values of the port (see SessionClient.batch_options)."""

    def __init__(self, client, options, *args, **kwargs):
        self._client = client
        self._options = options
        super().__init__(*args, **kwargs)

    def compose(self):
        options = self._options
        with Horizontal():
            yield Button("<", classes="mini view-dashboard").with_tooltip(
                "view dashboard"
            )
            yield PaneSearch(
                self._client.apply_filter,
                placeholder="filter tasks...",
                input_id="filter-input",
            )

            with Horizontal(classes="checkboxes"):
                with Vertical(classes="column"):
                    yield Checkbox("hide completed", id="hide-completed")
                    yield Checkbox("hide skipped", id="hide-skipped")
                with Vertical(classes="column"):
                    yield Checkbox(
                        "force run", value=options["force_run"], id="force-run"
                    )
                    yield Checkbox(
                        "force success",
                        value=options["force_success"],
                        id="force-success",
                    )
                with Vertical(classes="column"):
                    yield Checkbox("dry run", value=options["dry_run"], id="dry-run")
                    yield Checkbox(
                        "no concurrency",
                        value=options["disable_concurrency"],
                        id="disable-concurrency",
                    )
            with Horizontal(classes="actions"):
                yield Button("run all", id="run-all", variant="error")
                yield Button("check all", id="check-all", variant="success")
