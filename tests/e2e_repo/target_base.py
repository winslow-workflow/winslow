"""Shared base for the E2E fixture workflows - imported by bare name, so it
must live at the repo root (see iter_dir_module_names' sys.path fallback)."""

from winslow import Task, Workflow
from winslow.constants import Mode
from winslow.runner.store import TaskStore


class StatusHistoryMixin:
    """Record every status of each key, seed included, on the write-order
    seam (see ReactiveDict._apply): bus dispatch order is undefined, so only
    the seam sees the transitions in write order."""

    def __init__(self, *args, **kwargs):
        self.history = {}
        super().__init__(*args, **kwargs)
        # The constructor seed bypasses set, so capture it here.
        for key, status in self.items():
            self.history[key] = [status]

    def _apply(self, key, status):
        self.history.setdefault(key, []).append(status)
        super()._apply(key, status)

    def assert_history_equals(self, item, expected):
        """One statement that tests the full status history of an item, with the
        seed, against `expected`."""
        actual = self.history.get(self._key(item), [])
        expected = list(expected)
        if actual != expected:
            raise AssertionError(
                f"{item}: expected history {[str(s) for s in expected]}, "
                f"got {[str(s) for s in actual]}"
            )


class HistoryTaskStore(StatusHistoryMixin, TaskStore):
    pass


def _value_key(key):
    """The target outlives the session, so it must not retain a task: a task
    key becomes the task label, which is unique per pipeline."""
    if isinstance(key, Task):
        return str(key)
    if isinstance(key, tuple):
        return tuple(_value_key(part) for part in key)
    return key


class TargetDict(dict):
    """The fixture world: keys normalize through _value_key, so a lookup with
    a task and a lookup with its label find the same entry."""

    def __setitem__(self, key, value):
        super().__setitem__(_value_key(key), value)

    def __getitem__(self, key):
        return super().__getitem__(_value_key(key))

    def __contains__(self, key):
        return super().__contains__(_value_key(key))

    def get(self, key, default=None):
        return super().get(_value_key(key), default)

    def __eq__(self, other):
        if isinstance(other, dict):
            other = {_value_key(k): v for k, v in other.items()}
        return dict(self) == other

    def __ne__(self, other):
        return not self.__eq__(other)


class TargetWorkflow(Workflow):
    """E2E fixture base - both modes get history-recording stores; each
    instance owns a fresh target dict its tasks write to."""

    store_classes = {
        Mode.HEADLESS: HistoryTaskStore,
        Mode.TUI: HistoryTaskStore,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The per-run "world": tasks mark work done here, checks read it back.
        # On workflow_config because that's the one object every task carries.
        self.workflow_config.target = TargetDict()

    @property
    def target(self):
        return self.workflow_config.target


class TargetTask(Task):
    """run() marks the task done in the workflow's target dict; check() reads
    it back, keyed by task instance. Pre-seed the target to simulate
    already-done work."""

    class Meta:
        abstract = True

    # Strict: the check requires the exact value run() writes (True), so a
    # foreign seed marker doesn't count as done. Non-strict: key existence is
    # enough - models checks that probe for an artifact without validating its
    # content. Since run() always writes True, a non-True marker surviving a
    # run is proof the task never really ran (and vice versa).
    strict_check = True

    @property
    def target(self):
        return self.workflow_config.target

    def is_eligible(self):
        return True

    def run(self):
        self.target[self] = True

    def check(self):
        if self.strict_check:
            return self.target.get(self) is True
        return self in self.target
