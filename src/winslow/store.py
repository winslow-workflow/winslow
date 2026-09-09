import threading
from typing import Type, Any

from winslow.events import Origin, TaskStatusEvent

_MISSING = object()


class ReactiveDict:
    """A status map whose writes publish on the session bus (see SessionBus).
    `current` is the only storage. Each write rebinds it to a new dict, so a
    bound snapshot stays consistent. Reads accept an item or its identity
    key. Iteration yields keys; the task index resolves a key back to the
    live item."""

    def __init__(self, bus, seed=None):
        self.bus = bus
        self.current = dict(seed) if seed else {}
        self._lock = threading.RLock()
        self._settled = threading.Condition(self._lock)

    @classmethod
    def _key(cls, item):
        return getattr(item, "identity_key", item)

    def __setitem__(self, item, value) -> None:
        self.set(item, value)

    def set(self, item, value, origin=Origin.RUN) -> None:
        """Write, and stamp the origin of the write on the event. A seed
        write names itself, so the persistence subscriber can return at once
        (see SessionPersistenceAdapter)."""
        key = self._key(item)
        with self._lock:
            # A redundant write is dropped completely. Observers must not see
            # a transition that did not occur.
            if self.current.get(key, _MISSING) == value:
                return
            self._apply(key, value)
            self._settled.notify_all()
        # The publish runs outside the lock, so a slow subscriber delays
        # only the writing thread (see SessionBus).
        self._publish(key, value, origin)

    def _apply(self, key, value) -> None:
        """The write-order seam: it runs under the lock, in write order. An
        observer that needs that order overrides it. Every other observer
        subscribes to the bus."""
        self.current = {**self.current, key: value}

    def __getitem__(self, item):
        return self.current[self._key(item)]

    def get(self, item, default=None):
        return self.current.get(self._key(item), default)

    def __contains__(self, item) -> bool:
        return self._key(item) in self.current

    def __iter__(self):
        return iter(self.current)

    def __len__(self) -> int:
        return len(self.current)

    def keys(self):
        return self.current.keys()

    def items(self):
        return self.current.items()

    def values(self):
        return self.current.values()

    def clear(self) -> None:
        with self._lock:
            self.current = {}

    def wait_for_state(self, predicate, timeout=None):
        """Block until predicate() is true or the timeout ends. The predicate is
        tested again at each write to the store. The lock is released during the
        wait, so a waiter never blocks a writer. Returns the last result of the
        predicate."""
        with self._settled:
            return self._settled.wait_for(predicate, timeout)

    def _publish(self, key, status, origin) -> None:
        self.bus.publish(TaskStatusEvent(key=key, status=status, origin=origin))


class BaseStore(ReactiveDict):
    item_class: Type[Any] = None  # A child class must set this
    status_class: Type[Any] = None  # A child class must set this

    def __init__(self, bus, items: list = None) -> None:
        if not self.item_class or not self.status_class:
            raise ValueError("Child classes must define item_class and status_class.")

        items = items or []

        super().__init__(
            bus, {self._key(item): self.status_class.INITIALIZED for item in items}
        )

    def set(self, item, status, origin=Origin.RUN) -> None:
        if not isinstance(item, self.item_class):
            raise TypeError(f"Expected {self.item_class}, got {type(item)}")
        if not isinstance(status, self.status_class):
            raise TypeError(f"Expected {self.status_class}, got {type(status)}")

        super().set(item, status, origin)
