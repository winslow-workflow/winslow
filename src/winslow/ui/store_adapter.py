from textual.message import Message


class StoreEvent(Message):
    """A port event that goes to the UI thread. The handler of the screen
    calls apply() to run the event on that thread."""

    def __init__(self, apply):
        super().__init__()
        self.apply = apply


class SessionLifecycleEvent(Message):
    """The message from the app-level port subscriptions to the app. It has
    its own type for a reason: a screen posts a StoreEvent, which goes up to
    the app. If the app also handled StoreEvent, it would apply the events of
    each screen a second time."""

    def __init__(self, apply):
        super().__init__()
        self.apply = apply
