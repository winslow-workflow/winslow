import threading
from typing import Type, Any

from winslow.util import ListenerMixin


class StoreListener:
    """Observer of the changes in a store. Subscribe with BaseStore.add_listener.

    A callback runs on the thread that changed the store, while that thread
    holds the store lock. A callback must therefore not block, and must not
    take a lock that another thread holds while it waits on this one. Each
    callback does nothing by default, so a listener overrides only the
    events that it needs.

    Example scenario, the TUI adapter (see TuiStoreAdapter):

        1. A worker thread completes a task and writes
           store[task] = COMPLETED. The write takes the store lock.
        2. The callback on_task_status runs on that worker thread, under
           the lock.
        3. The adapter posts the event to the UI thread and returns
           immediately. The worker releases the lock and continues.

    A slow body in step 3, for example a synchronous render of the task
    table, stalls the runner at each write. A wait on the UI thread can
    deadlock: the worker holds the store lock and waits for the UI thread,
    while the UI thread waits for the store lock to read a status.

    Every payload is a value, never a live task. A task event carries the
    identity key of the task (see Task.identity_key). A batch event carries
    the ExecutionBatch, which holds no task."""

    def on_task_status(self, key, status):
        pass

    def on_execution_status(self, task_key, status, batch_uuid):
        pass

    def on_batch_created(self, batch):
        pass

    def on_batch_completed(self, batch):
        pass

    def on_log_appended(self, task_key, batch_uuid, line):
        pass


_MISSING = object()


class ReactiveDict(ListenerMixin, dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = threading.RLock()
        self._settled = threading.Condition(self._lock)
        self._init_listeners()

    def callback(self, key, value):
        """Hook that runs after a write. A subclass overrides it, for example to
        log the status."""

    def __setitem__(self, key, value) -> None:
        self.set(key, value)

    def set(self, key, value, excluded_callbacks=None) -> None:
        """Write, with an optional per-write listener exclusion. A replay of
        a persisted value excludes its persistence listener; every other
        listener still sees the write (see SessionPersistenceAdapter)."""
        with self._lock:
            # A redundant write is dropped completely. The dict write is cheap,
            # but the callback and the listeners can be slow (UI adapters).
            # Observers must not see a transition that did not occur.
            if self.get(key, _MISSING) == value:
                return
            super().__setitem__(key, value)
            # The callback and the listeners run under the lock. This is safe
            # while no callback blocks, and while no callback takes a second lock
            # that a thread holds while it waits on this one (see StoreListener).
            self.callback(key, value)
            self._emit_status(key, value, excluded_callbacks)
            self._settled.notify_all()

    def wait_for_state(self, predicate, timeout=None):
        """Block until predicate() is true or the timeout ends. The predicate is
        tested again at each write to the store. The lock is released during the
        wait, so a waiter never blocks a writer. Returns the last result of the
        predicate."""
        with self._settled:
            return self._settled.wait_for(predicate, timeout)

    def _emit_status(self, item, status, excluded_callbacks=None) -> None:
        # The listener payload is the identity key, never the live item. The
        # derivation is a cached-attribute read, cheap under the store lock.
        self._emit(
            StoreListener.on_task_status,
            getattr(item, "identity_key", item),
            status,
            excluded=excluded_callbacks,
        )

    def emit_batch_created(self, batch) -> None:
        self._emit(StoreListener.on_batch_created, batch)

    def emit_batch_completed(self, batch) -> None:
        self._emit(StoreListener.on_batch_completed, batch)


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

    def __init__(self, items: list = None) -> None:
        if not self.item_class or not self.status_class:
            raise ValueError("Child classes must define item_class and status_class.")

        items = items or []

        super().__init__({item: self.status_class.INITIALIZED for item in items})

    def set(self, item, status, excluded_callbacks=None) -> None:
        if not isinstance(item, self.item_class):
            raise TypeError(f"Expected {self.item_class}, got {type(item)}")
        if not isinstance(status, self.status_class):
            raise TypeError(f"Expected {self.status_class}, got {type(status)}")

        super().set(item, status, excluded_callbacks)
