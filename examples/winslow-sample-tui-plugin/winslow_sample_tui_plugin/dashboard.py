from textual.containers import Center
from textual.widgets import Static

from winslow.ui.plugin import Slots, UIPlugin


class SampleWidget(Center):
    def compose(self):
        yield Static("Hello from sample plugin!")


class SampleDashboardPlugin(UIPlugin):
    slot = Slots.DASHBOARD_WORKFLOWS
    label = "Sample"

    def create_widget(self, context):
        return SampleWidget()
