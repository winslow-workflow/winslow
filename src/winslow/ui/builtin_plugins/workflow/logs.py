from textual import on
from textual.message import Message

from winslow.model import SessionLogEvent
from winslow.ui.plugin import UIPlugin, RenderContext, Slots
from winslow.ui.widgets.common import LogView


class SessionLogView(LogView):
    """The session log pane. It subscribes to the session log lane of the
    port, then writes the served backlog: a line in that window duplicates
    rather than disappears."""

    class Line(Message):
        def __init__(self, line):
            self.line = line
            super().__init__()

    def __init__(self, client, *args, **kwargs):
        self._client = client
        super().__init__(*args, **kwargs)

    def _on_session_log(self, event):
        # The port publishes on an arbitrary thread; the message hops to the
        # UI thread.
        self.post_message(self.Line(event.line))

    @on(Line)
    def _write_line(self, message):
        self.write(message.line)

    def on_mount(self):
        self._client.subscribe(SessionLogEvent, self._on_session_log)
        for line in self._client.snapshot().session_log_backlog:
            self.write(line)

    def on_unmount(self):
        self._client.unsubscribe(SessionLogEvent, self._on_session_log)


class WorkflowLogsPlugin(UIPlugin):
    slot = Slots.WORKFLOW_LOGS
    label = "Logs"

    def create_widget(self, context: RenderContext):
        return SessionLogView(client=context.client)
