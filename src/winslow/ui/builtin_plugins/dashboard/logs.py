from textual import on
from textual.message import Message

from winslow.ui.plugin import UIPlugin, RenderContext, Slots
from winslow.ui.widgets.common import LogView
from winslow.logger import InteractiveLogHandler


class AppLogView(LogView):
    """The log of the process the TUI runs in: a local-only pane by nature
    (see docs/ui-plugins.md). It attaches to the app logger at mount."""

    class Line(Message):
        def __init__(self, line):
            self.line = line
            super().__init__()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._handler = InteractiveLogHandler(self._on_line)

    def _on_line(self, line):
        # The logger emits on the thread that logs; the message hops to the
        # UI thread.
        self.post_message(self.Line(line))

    @on(Line)
    def _write_line(self, message):
        self.write(message.line)

    def on_mount(self):
        self.app.logger.addHandler(self._handler)

    def on_unmount(self):
        self.app.logger.removeHandler(self._handler)


class DashboardLogsPlugin(UIPlugin):
    slot = Slots.DASHBOARD_LOGS
    label = "Logs"

    def create_widget(self, context: RenderContext):
        return AppLogView(highlight=True)
