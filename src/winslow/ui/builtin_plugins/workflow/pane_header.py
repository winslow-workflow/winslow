from textual.containers import Horizontal
from textual.widgets import Input, Button

from winslow.ui.filter_validation import FilterSyntaxValidator


class PaneSearch(Horizontal):
    """The three search controls that the headers of the Tasks pane and the
    History pane share: the filter input, the syntax help button and the
    parameters button. _pane_header.tcss holds their style."""

    DEFAULT_CLASSES = "search"

    def __init__(self, workflow, placeholder, input_id, *args, **kwargs):
        self.workflow = workflow
        self.placeholder = placeholder
        self.input_id = input_id
        super().__init__(*args, **kwargs)

    def compose(self):
        yield Input(
            placeholder=self.placeholder,
            id=self.input_id,
            validators=[FilterSyntaxValidator(self.workflow.filter_registry)],
            validate_on=[],
        )
        yield Button("?", classes="search-help")
        yield Button("⚙", classes="workflow-params").with_tooltip("Parameters")
