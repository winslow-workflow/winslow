"""Layer B - the render seam: a plugin's create_widget builds a widget from its
RenderContext. This constructs the widget only (no mount, no app); layout and
interaction are not asserted headlessly."""

from types import SimpleNamespace

from textual.widgets import Label

from winslow.ui.plugin import Slots, UIPlugin, WorkflowRenderContext


class Greeter(UIPlugin):
    name = "greeter"
    slot = Slots.TASK_OVERVIEW

    def create_widget(self, context):
        # Encode the context into the widget id - a public attribute readable
        # without mounting, so the test proves the wiring without rendering.
        return Label("tasks", id=f"task-count-{len(context.roster)}")


def test_create_widget_builds_from_context():
    context = WorkflowRenderContext(
        client=SimpleNamespace(),
        session=SimpleNamespace(),
        snapshot=SimpleNamespace(),
        roster=(1, 2, 3),
    )
    widget = Greeter().create_widget(context)

    assert isinstance(widget, Label)
    assert widget.id == "task-count-3"
