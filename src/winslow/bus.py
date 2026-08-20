"""The session event bus: one event path per session. A publisher calls
publish(event), and a subscriber connects a callback to an event class (see
winslow.events). No blinker import appears outside this module."""

import threading

from blinker import Signal

from winslow.exceptions import RegistrationError
from winslow.logger import LOGGER


class _OrderedIds(dict):
    """An insertion-ordered set for the receiver bookkeeping of blinker.
    Dispatch order is then subscription order (see Signal.set_class)."""

    def add(self, item):
        self[item] = None

    def discard(self, item):
        self.pop(item, None)


class _OrderedSignal(Signal):
    set_class = _OrderedIds


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

    publish dispatches synchronously on the calling thread. For a store event
    that thread holds the store lock. A callback must not block, and must not
    take a lock that another thread holds while it waits on this one.

    Example scenario, the TUI adapter (see TuiStoreAdapter):

        1. A worker thread completes a task and writes
           store[task] = COMPLETED. The write takes the store lock.
        2. The TaskStatusEvent callback runs on that worker thread, under
           the lock.
        3. The adapter posts the event to the UI thread and returns
           immediately. The worker releases the lock and continues.

    A slow body in step 3, for example a synchronous render of the task
    table, stalls the runner at each write. A wait on the UI thread can
    deadlock: the worker holds the store lock and waits for the UI thread,
    while the UI thread waits for the store lock to read a status."""

    def __init__(self):
        # The lock guards the subscription table. publish runs outside the
        # lock: blinker iterates a snapshot, so a subscriber can unsubscribe
        # from another thread during a dispatch.
        self._lock = threading.Lock()
        self._signals = {}
        self._receivers = {}
        self._closed = False

    def _signal_of(self, event_class):
        # Only under the lock.
        if event_class not in self._signals:
            self._signals[event_class] = _OrderedSignal()
        return self._signals[event_class]

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
            if (event_class, callback) in self._receivers:
                raise RegistrationError(
                    f"{callback!r} is already subscribed to "
                    f"{event_class.__name__}. Unsubscribe it before a second "
                    f"subscription."
                )
            self._receivers[(event_class, callback)] = receiver
            self._signal_of(event_class).connect(receiver, weak=False)

    def unsubscribe(self, event_class, callback):
        """Disconnect the callback. An unknown callback is a no-op, so a
        teardown path can run twice."""
        with self._lock:
            receiver = self._receivers.pop((event_class, callback), None)
            if receiver is not None:
                self._signals[event_class].disconnect(receiver)

    def publish(self, event):
        """Dispatch the event to its subscribers, on the calling thread, in
        subscription order. A publish on a closed bus is a no-op: the session
        end can race a draining worker."""
        with self._lock:
            signal = self._signals.get(type(event))
        if signal is not None:
            signal.send(self, event=event)

    def close(self):
        """Disconnect every remaining subscriber. The session end calls this
        after the SessionEndedEvent dispatch. This method is idempotent."""
        with self._lock:
            for (event_class, _), receiver in self._receivers.items():
                self._signals[event_class].disconnect(receiver)
            self._receivers.clear()
            self._closed = True
