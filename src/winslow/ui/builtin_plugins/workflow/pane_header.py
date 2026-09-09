from textual.containers import Horizontal
from textual.widgets import Input, Button

from winslow.filter.builtin import parse_syntax
from winslow.ui.filter_validation import FilterSyntaxValidator


class PaneSearch(Horizontal):
    """The three search controls that the Tasks pane and the History pane
    share: the filter input, the syntax help button and the parameters
    button. The validator checks the grammar alone; the matcher resolves the
    command vocabulary server-side (see parse_syntax)."""

    DEFAULT_CLASSES = "search"

    def __init__(self, placeholder, input_id, *args, **kwargs):
        self.placeholder = placeholder
        self.input_id = input_id
        super().__init__(*args, **kwargs)

    def compose(self):
        yield Input(
            placeholder=self.placeholder,
            id=self.input_id,
            validators=[FilterSyntaxValidator(parse_syntax)],
            validate_on=[],
        )
        yield Button("?", classes="search-help")
        yield Button("⚙", classes="workflow-params").with_tooltip("Parameters")
