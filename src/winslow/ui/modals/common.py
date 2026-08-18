from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Label, Button
from textual.containers import Horizontal, Vertical

from winslow.ui.css import package_css


class ModalHeader(Widget):
    DEFAULT_CLASSES = "modal-header"

    def __init__(self, text, *args, **kwargs):
        self.text = text
        super().__init__(*args, **kwargs)

    def compose(self):
        with Horizontal():
            with Horizontal(classes="label-container"):
                yield Label(self.text)
            with Horizontal(classes="button-container"):
                yield Button("Close", classes="small", id="close-modal")

    async def on_button_pressed(self, event):
        if event.button.id == "close-modal":
            event.stop()
            self.app.pop_screen()  # Close the modal screen


class BaseModal(ModalScreen):
    """The shared shell of a modal. It is centered, the escape key closes it, and
    its content is in a .modal-content Vertical below a ModalHeader. A subclass
    supplies modal_title and compose_content. CONTENT_CLASSES adds the size
    classes of each modal. The shell style lives in base_modal.tcss at the
    DEFAULT_CSS tier, so a plugin modal overrides it with its own DEFAULT_CSS."""

    DEFAULT_CSS = package_css(__package__, "base_modal.tcss")
    DEFAULT_CLASSES = "centered"
    BINDINGS = [("escape", "dismiss", "Close")]
    CONTENT_CLASSES = ""

    @property
    def modal_title(self):
        raise NotImplementedError

    def compose_content(self):
        raise NotImplementedError

    def compose(self):
        with Vertical(classes=f"modal-content {self.CONTENT_CLASSES}".strip()):
            yield ModalHeader(self.modal_title)
            yield from self.compose_content()
