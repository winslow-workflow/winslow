from textual.widgets import Static

from winslow.ui.plugin import Slot, Slots

# Distinct hues for neighbour slots. The alpha keeps the pane readable under
# the cover.
COLORS = ("red", "blue", "green", "magenta", "orange", "cyan")


def iter_slots():
    return [s for s in vars(Slots).values() if isinstance(s, Slot)]


class SlotHighlight(Static):
    """A semi-transparent cover for one slot container. It shows the slot id
    in the middle. The slot inspector mounts one cover per slot (see
    SlottedScreen.action_toggle_slot_inspector)."""

    DEFAULT_CSS = """
    SlotHighlight {
        position: absolute;
        offset: 0 0;
        width: 100%;
        height: 100%;
        content-align: center middle;
        text-style: bold;
        color: auto;
    }
    """

    def __init__(self, slot_id, color):
        super().__init__(slot_id)
        self.styles.background = f"{color} 40%"
