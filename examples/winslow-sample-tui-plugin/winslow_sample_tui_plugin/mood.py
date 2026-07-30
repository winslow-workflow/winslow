from textual.widgets import Static

from winslow.ui.builtin_plugins.common.system_resources import (
    SystemStats,
    CpuStat,
    MemoryStat,
)
from winslow.ui.builtin_plugins.dashboard.resources import (
    DashboardSystemResourcesPlugin,
)


def _face(pct):
    if pct < 30:
        return "😊"
    if pct < 70:
        return "😰"
    return "😡"


class MoodSystemStats(SystemStats):
    def compose(self):
        yield Static(_face(0.0), id="mood-face")
        yield from super().compose()

    def on_mount(self):
        self.set_interval(2, self._update_mood)

    def _update_mood(self):
        cpu = self.query_one(CpuStat).percentage
        mem = self.query_one(MemoryStat).percentage
        self.query_one("#mood-face", Static).update(_face(max(cpu, mem)))


class ResourcesMoodPlugin(DashboardSystemResourcesPlugin):
    label = "System Mood"
    replace = "builtin.dashboard-system-resources-plugin"

    def create_widget(self, context):
        return MoodSystemStats()
