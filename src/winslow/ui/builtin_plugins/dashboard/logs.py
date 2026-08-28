from winslow.ui.plugin import UIPlugin, RenderContext, Slots
from winslow.ui.widgets.common import LogView
from winslow.logger import InteractiveLogHandler


class AppLogView(LogView):
    """The log of the process the TUI runs in: a local-only pane by nature
    (see docs/ui-plugins.md). It attaches to the app logger at mount."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._handler = InteractiveLogHandler(self.write)

    def on_mount(self):
        self.app.logger.addHandler(self._handler)

    def on_unmount(self):
        self.app.logger.removeHandler(self._handler)


class DashboardLogsPlugin(UIPlugin):
    slot = Slots.DASHBOARD_LOGS
    label = "Logs"

    def create_widget(self, context: RenderContext):
        return AppLogView(highlight=True)
