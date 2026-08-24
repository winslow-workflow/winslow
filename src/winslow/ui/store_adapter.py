from functools import partial, wraps

from textual.message import Message

from winslow.cache import CacheListener
from winslow.events import (
    BatchCompletedEvent,
    BatchCreatedEvent,
    ExecutionStatusEvent,
    LogLineEvent,
    SessionEndedEvent,
    TaskStatusEvent,
)


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


class TuiStoreAdapter:
    """Send the bus events to the Textual UI: the proxy between the session
    bus and the Textual messages. The bus thus has no dependency on Textual."""

    def __init__(self, app, screen_name):
        self.app = app
        self.screen_name = screen_name

    @property
    def _screen(self):
        return self.app.get_screen(self.screen_name)

    def attach(self, workflow):
        """Wire each handler onto its session event. The bus close at the
        session end disconnects the adapter (see Workflow.archive_state)."""
        for event, handler in (
            (TaskStatusEvent, self.on_task_status),
            (ExecutionStatusEvent, self.on_execution_status),
            (BatchCreatedEvent, self.on_batch_created),
            (BatchCompletedEvent, self.on_batch_completed),
            (LogLineEvent, self.on_log_line),
            (SessionEndedEvent, self.on_session_ended),
        ):
            workflow.subscribe(event, handler)

    @on_ui_thread
    def on_task_status(self, event):
        self._screen.propagate_task_status(event.key, event.status)

    @on_ui_thread
    def on_execution_status(self, event):
        self._screen.propagate_execution_status(
            event.task_key, event.status, event.batch_uuid
        )

    @on_ui_thread
    def on_batch_created(self, event):
        self._screen.propagate_batch_created(event.batch)

    @on_ui_thread
    def on_batch_completed(self, event):
        self._screen.propagate_batch_completed(event.batch)

    @on_ui_thread
    def on_log_line(self, event):
        self._screen.propagate_task_log(event.task_key, event.batch_uuid, event.line)

    @on_ui_thread
    def on_session_ended(self, event):
        self._screen.propagate_session_ended()


class TuiCacheAdapter(CacheListener):
    """Send the cache events to the Textual UI as one repaint trigger: the
    pane re-peeks, so the payloads stay unused (see CacheUpdated). Remove the
    adapter from both containers at session end."""

    def __init__(self, app, screen_name):
        self.app = app
        self.screen_name = screen_name

    @property
    def _screen(self):
        return self.app.get_screen(self.screen_name)

    @on_ui_thread
    def on_entry_computed(self, info, previous_state):
        self._screen.propagate_cache_update()

    @on_ui_thread
    def on_entries_invalidated(self, scope, dropped, trigger):
        self._screen.propagate_cache_update()

    @on_ui_thread
    def on_eager_population_started(self, scope, entries):
        self._screen.propagate_cache_update()

    @on_ui_thread
    def on_eager_population_finished(self, scope, entries):
        self._screen.propagate_cache_update()

    @on_ui_thread
    def on_entry_error(self, scope, cache_name, entry_name, error):
        self._screen.propagate_cache_update()


class SessionLifecycleEvent(Message):
    """The message from the lifecycle adapter to the app. It has its own type for
    a reason: a screen posts a StoreEvent, which goes up to the app. If the app
    also handled StoreEvent, it would apply the events of each screen a second
    time."""

    def __init__(self, apply):
        super().__init__()
        self.apply = apply


class SessionLifecycleAdapter:
    """The transport for the drain rule (Session.finalize_if_drained). A batch
    completes on a worker thread, but the finalization clears the store, so it
    must run on the UI thread. It is thus serialized with each widget that reads
    the store."""

    def __init__(self, app, session):
        self.app = app
        self.session = session

    def attach(self, workflow):
        workflow.subscribe(BatchCompletedEvent, self.on_batch_completed)

    def on_batch_completed(self, event):
        self.app.post_message(SessionLifecycleEvent(self.session.finalize_if_drained))
