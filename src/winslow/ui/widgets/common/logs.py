from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import RichLog

from rich.highlighter import ReprHighlighter

from winslow.logger import InteractiveLogHandler


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


class SyncLog(LogView):
    def __init__(self, logger, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._logger = logger
        self._handler = InteractiveLogHandler(self.write)
        logger.addHandler(self._handler)

    def on_unmount(self):
        self._logger.removeHandler(self._handler)


class InlineLog(Widget):
    content = reactive("")

    def render(self):
        return HIGHLIGHTER(self.content)
