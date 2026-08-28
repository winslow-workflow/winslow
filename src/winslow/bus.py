"""The session event bus: one event path per session. A publisher calls
publish(event), and a subscriber connects a callback to an event class (see
winslow.events). No blinker import appears outside this module."""

import threading

from blinker import Signal

from winslow.events import (
    BatchCompletedEvent,
    BatchCreatedEvent,
    BatchOptionsChangedEvent,
    ExecutionStatusEvent,
    LogLineEvent,
    SessionEndedEvent,
    TaskStatusEvent,
)
from winslow.exceptions import RegistrationError
from winslow.logger import LOGGER


def _isolate(callback):
    """Wrap the callback, so a raise is logged and the dispatch continues:
    an observer must not break the operation it observes. The wrapper also
    reduces the blinker receiver signature to the one event argument."""

    def receiver(sender, *, event):
        try:
            callback(event)
        except Exception:
            LOGGER.error(
                f"Subscriber {callback!r} failed on {type(event).__name__}.",
                exc_info=True,
            )

    return receiver


class SessionBus:
    """One bus per session. Every component that observes the session
    subscribes here, by event class, and the session-end sweep disconnects
    every remaining subscriber (see close).

    publish dispatches synchronously on the calling thread, outside the store
    lock (see ReactiveDict.set). Dispatch order between events is undefined:
    a callback that renders state reads the store for the latest view.
    A callback must return immediately, because it runs on the thread that
    produced the event.

    Example scenario, the workflow screen of the TUI (see WorkflowScreen):

        1. A worker thread completes a task and writes
           store[task] = COMPLETED, then publishes with the lock released.
        2. The TaskStatusEvent callback runs on that worker thread.
        3. The screen handler posts the event to the UI thread and returns
           immediately. The worker continues.

    A slow body in step 3, for example a synchronous render of the task
    table, stalls that worker at each write."""

    # The event vocabulary of the bus. A subclass overrides this to extend it.
    event_classes = (
        TaskStatusEvent,
        ExecutionStatusEvent,
        BatchCreatedEvent,
        BatchCompletedEvent,
        LogLineEvent,
        SessionEndedEvent,
        BatchOptionsChangedEvent,
    )

    @classmethod
    def get_event_classes(cls):
        """The declared events of this bus (see event_classes)."""
        return cls.event_classes

    def __init__(self):
        # The lock guards the subscription table. publish runs outside the
        # lock: blinker iterates a snapshot, so a subscriber can unsubscribe
        # from another thread during a dispatch.
        self._lock = threading.Lock()
        self._signals = {
            event_class: Signal() for event_class in self.get_event_classes()
        }
        self._receivers = {}
        self._closed = False

    def _refuse_undeclared(self, event_class):
        raise RegistrationError(
            f"{event_class.__name__} is not a declared event of this bus. "
            f"The declared events are "
            f"{sorted(k.__name__ for k in self.get_event_classes())}. "
            f"A subclass extends event_classes."
        )

    def subscribe(self, event_class, callback):
        """Connect the callback to the event class. The bus holds the callback
        strongly until unsubscribe or close, so a bound method stays alive."""
        receiver = _isolate(callback)
        with self._lock:
            if self._closed:
                raise RegistrationError(
                    f"The session bus is closed - it accepts no subscription "
                    f"to {event_class.__name__}. Subscribe before the session "
                    f"ends."
                )
            if event_class not in self._signals:
                self._refuse_undeclared(event_class)
            if (event_class, callback) in self._receivers:
                raise RegistrationError(
                    f"{callback!r} is already subscribed to "
                    f"{event_class.__name__}. Unsubscribe it before a second "
                    f"subscription."
                )
            self._receivers[(event_class, callback)] = receiver
            self._signals[event_class].connect(receiver, weak=False)

    def unsubscribe(self, event_class, callback):
        """Disconnect the callback. An unknown callback is a no-op, so a
        teardown path can run twice."""
        with self._lock:
            receiver = self._receivers.pop((event_class, callback), None)
            if receiver is not None:
                self._signals[event_class].disconnect(receiver)

    def publish(self, event):
        """Dispatch the event to its subscribers, on the calling thread. The
        dispatch order is undefined: a subscriber must not depend on another
        subscriber. A publish on a closed bus is a no-op: the session end can
        race a draining worker."""
        with self._lock:
            if self._closed:
                return
            signal = self._signals.get(type(event))
        if signal is None:
            self._refuse_undeclared(type(event))
        signal.send(self, event=event)

    def close(self):
        """Disconnect every remaining subscriber. The session end calls this
        after the SessionEndedEvent dispatch. This method is idempotent."""
        with self._lock:
            for (event_class, _), receiver in self._receivers.items():
                self._signals[event_class].disconnect(receiver)
            self._receivers.clear()
            # An empty table frees the signals with the session.
            self._signals.clear()
            self._closed = True
