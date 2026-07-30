from importlib.resources import files

from textual.widgets import Markdown
from textual.containers import VerticalScroll

from .common import BaseModal

SYNTAX_MD = files("winslow.filter").joinpath("syntax.md").read_text()


class FilterHelp(BaseModal):
    modal_title = "Filter Syntax"

    def compose_content(self):
        with VerticalScroll():
            yield Markdown(SYNTAX_MD)
