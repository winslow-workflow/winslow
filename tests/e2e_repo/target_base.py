"""Shared base for the E2E fixture workflows - imported by bare name, so it
must live at the repo root (see iter_dir_module_names' sys.path fallback)."""

from winslow import Task, Workflow
from winslow.constants import Mode
from winslow.runner.store import TaskStore, InteractiveStore
from winslow.store import StatusHistoryMixin


class HistoryTaskStore(StatusHistoryMixin, TaskStore):
    pass


class HistoryInteractiveStore(StatusHistoryMixin, InteractiveStore):
    pass


class TargetWorkflow(Workflow):
    """E2E fixture base - both modes get history-recording stores; each
    instance owns a fresh target dict its tasks write to."""

    store_classes = {
        Mode.HEADLESS: HistoryTaskStore,
        Mode.TUI: HistoryInteractiveStore,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The per-run "world": tasks mark work done here, checks read it back.
        # On workflow_config because that's the one object every task carries.
        self.workflow_config.target = {}

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
