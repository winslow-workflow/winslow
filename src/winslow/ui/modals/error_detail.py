from rich.highlighter import ReprHighlighter

from textual.widgets import Static
from textual.containers import VerticalScroll

from .common import BaseModal


class ErrorDetail(BaseModal):
    modal_title = "Workflow init failed"

    def __init__(self, error, *args, **kwargs):
        self._error = error
        super().__init__(*args, **kwargs)

    def compose_content(self):
        with VerticalScroll():
            yield Static(self._traceback())

    def _traceback(self):
        if not self._error:
            return "No details available."
        return ReprHighlighter()(self._error)
