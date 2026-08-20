import threading
from typing import Type, Any

from winslow.events import Origin, TaskStatusEvent

_MISSING = object()


class ReactiveDict(dict):
    """A dict whose writes publish on the session bus (see SessionBus). The
    bus owns the dispatch contract: synchronous, on the writing thread, under
    the store lock."""

    def __init__(self, bus, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bus = bus
        self._lock = threading.RLock()
        self._settled = threading.Condition(self._lock)

    def callback(self, key, value):
        """Hook that runs after a write. A subclass overrides it, for example to
        log the status."""

    def __setitem__(self, key, value) -> None:
        self.set(key, value)

    def set(self, key, value, origin=Origin.RUN) -> None:
        """Write, and stamp the origin of the write on the event. A replay or
        a seed write names itself, so the persistence subscriber can return
        at once (see SessionPersistenceAdapter)."""
        with self._lock:
            # A redundant write is dropped completely. The dict write is cheap,
            # but the callback and the subscribers can be slow (UI adapters).
            # Observers must not see a transition that did not occur.
            if self.get(key, _MISSING) == value:
                return
            super().__setitem__(key, value)
            # The callback and the publish run under the lock. This is safe
            # while no subscriber blocks, and while no subscriber takes a
            # second lock that a thread holds while it waits on this one
            # (see SessionBus).
            self.callback(key, value)
            self._publish(key, value, origin)
            self._settled.notify_all()

    def wait_for_state(self, predicate, timeout=None):
        """Block until predicate() is true or the timeout ends. The predicate is
        tested again at each write to the store. The lock is released during the
        wait, so a waiter never blocks a writer. Returns the last result of the
        predicate."""
        with self._settled:
            return self._settled.wait_for(predicate, timeout)

    def _publish(self, item, status, origin) -> None:
        # The event payload is the identity key, never the live item. The
        # derivation is a cached-attribute read, cheap under the store lock.
        self.bus.publish(
            TaskStatusEvent(
                key=getattr(item, "identity_key", item),
                status=status,
                origin=origin,
            )
        )


class StatusHistoryMixin:
    """Record every status of each key, seed included. The key is the identity
    key of the item, or str(item): an item key would retain each task past
    release_tasks."""

    @classmethod
    def _history_key(cls, item):
        return getattr(item, "identity_key", None) or str(item)

    def __init__(self, *args, **kwargs):
        self.history = {}
        super().__init__(*args, **kwargs)
        # A store that the plain dict constructor seeds, for example a per-batch
        # record store, does not call __setitem__. Capture the seed here.
        for item, status in self.items():
            self.history[self._history_key(item)] = [status]

    def callback(self, item, status):
        self.history.setdefault(self._history_key(item), []).append(status)
        super().callback(item, status)

    def assert_history_equals(self, item, expected):
        """One statement that tests the full status history of an item, with the
        seed, against `expected`."""
        actual = self.history.get(self._history_key(item), [])
        expected = list(expected)
        if actual != expected:
            raise AssertionError(
                f"{item}: expected history {[str(s) for s in expected]}, "
                f"got {[str(s) for s in actual]}"
            )


class BaseStore(ReactiveDict):
    item_class: Type[Any] = None  # A child class must set this
    status_class: Type[Any] = None  # A child class must set this

    def __init__(self, bus, items: list = None) -> None:
        if not self.item_class or not self.status_class:
            raise ValueError("Child classes must define item_class and status_class.")

        items = items or []

        super().__init__(bus, {item: self.status_class.INITIALIZED for item in items})

    def set(self, item, status, origin=Origin.RUN) -> None:
        if not isinstance(item, self.item_class):
            raise TypeError(f"Expected {self.item_class}, got {type(item)}")
        if not isinstance(status, self.status_class):
            raise TypeError(f"Expected {self.status_class}, got {type(status)}")

        super().set(item, status, origin)
