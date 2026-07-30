from functools import partial, wraps

from textual.message import Message

from winslow.store import StoreListener


class StoreEvent(Message):
    """A store event that goes to the UI thread. The handler of the screen calls
    apply() to run the event on that thread."""

    def __init__(self, apply):
        super().__init__()
        self.apply = apply


def on_ui_thread(method):
    """Move the body of the callback to the UI thread. The wrapper posts it to the
    screen and returns immediately, so an emit from the store never waits for the
    UI."""

    @wraps(method)
    def wrapper(self, *args):
        self._screen.post_message(StoreEvent(partial(method, self, *args)))

    return wrapper


class TuiStoreAdapter(StoreListener):
    """Send the store events to the Textual UI. The store thus has no dependency
    on Textual."""

    def __init__(self, app, screen_name):
        self.app = app
        self.screen_name = screen_name

    @property
    def _screen(self):
        return self.app.get_screen(self.screen_name)

    @on_ui_thread
    def on_task_status(self, task, status):
        self._screen.propagate_task_status(task, status)

    @on_ui_thread
    def on_execution_status(self, task, status, batch_uuid):
        self._screen.propagate_execution_status(task, status, batch_uuid)

    @on_ui_thread
    def on_batch_created(self, batch):
        self._screen.propagate_batch_created(batch)

    @on_ui_thread
    def on_batch_completed(self, batch):
        self._screen.propagate_batch_completed(batch)

    @on_ui_thread
    def on_log_appended(self, task, batch_uuid, line):
        self._screen.propagate_task_log(task, batch_uuid, line)


class SessionLifecycleEvent(Message):
    """The message from the lifecycle adapter to the app. It has its own type for
    a reason: a screen posts a StoreEvent, which goes up to the app. If the app
    also handled StoreEvent, it would apply the events of each screen a second
    time."""

    def __init__(self, apply):
        super().__init__()
        self.apply = apply


class SessionLifecycleAdapter(StoreListener):
    """The transport for the drain rule (Session.finalize_if_drained). A batch
    completes on a worker thread, but the finalization clears the store, so it
    must run on the UI thread. It is thus serialized with each widget that reads
    the store."""

    def __init__(self, app, session):
        self.app = app
        self.session = session

    def on_batch_completed(self, batch):
        self.app.post_message(SessionLifecycleEvent(self.session.finalize_if_drained))
