from textual.containers import Container, VerticalScroll
from textual.widget import Widget
from textual.widgets import Static

from winslow.ui.plugin import UIPlugin, RenderContext, Slots


class SessionListWidget(Widget):
    """A list of session rows with a border. It scrolls and shows a placeholder
    when it is empty."""

    DEFAULT_CLASSES = "round"
    LIST_ID = ""
    EMPTY_TEXT = ""

    def compose(self):
        yield Container(
            Static(self.EMPTY_TEXT),
            id=f"{self.LIST_ID}-placeholder",
            classes="centered",
        )
        yield VerticalScroll(classes="hidden", id=self.LIST_ID)


class SessionsWidget(SessionListWidget):
    LIST_ID = "session-list"
    EMPTY_TEXT = "No sessions created yet."


class HistoryWidget(SessionListWidget):
    LIST_ID = "history-list"
    EMPTY_TEXT = "No ended sessions yet."


class DashboardSessionsPlugin(UIPlugin):
    slot = Slots.DASHBOARD_SESSIONS
    label = "Sessions"
    priority = 5

    def create_widget(self, context: RenderContext):
        return SessionsWidget()


class DashboardHistoryPlugin(UIPlugin):
    slot = Slots.DASHBOARD_SESSIONS
    label = "History"
    priority = DashboardSessionsPlugin.priority + 1

    def create_widget(self, context: RenderContext):
        return HistoryWidget()
