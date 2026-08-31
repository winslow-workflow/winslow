from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import RichLog

from rich.highlighter import ReprHighlighter


HIGHLIGHTER = ReprHighlighter()


class LogView(RichLog):
    """The base widget that shows the logs. It holds the shared RichLog defaults.
    It has no CSS: put the style in a .tcss file."""

    DEFAULT_CLASSES = "no-overflow-x"

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("wrap", True)
        kwargs.setdefault("markup", True)
        kwargs.setdefault("highlight", True)
        super().__init__(*args, **kwargs)


class InlineLog(Widget):
    content = reactive("")

    def render(self):
        return HIGHLIGHTER(self.content)
