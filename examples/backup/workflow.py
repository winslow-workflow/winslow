from pathlib import Path

from winslow import (
    ClassConstraint,
    ConfigOption,
    Constraint,
    ConstraintType,
    Task,
    Workflow,
)

STATE = Path("state")


class Backup(Workflow):
    """A small backup flow. It shows one constraint for each constraint list.
    Delete the state directory to run it again."""

    maintenance = ConfigOption(
        action="store_true", default=False, help_text="Block the upload task."
    )
    offline = ConfigOption(
        action="store_true",
        default=False,
        help_text="Block the upload run and the verify check.",
    )


def marker(task):
    return STATE / f"{task}.done"


# -- constraints ---------------------------------------------------------------


class SnapshotPresent(Constraint):
    """A success rule. The task that declares it needs no check method."""

    valid_types = ConstraintType.SUCCESS

    def apply(self, task):
        return marker(task).exists()


class NotInMaintenance(Constraint):
    """A runnability rule. It reads the workflow config."""

    valid_types = ConstraintType.RUNNABILITY

    def apply(self, task):
        return not self.workflow_config.maintenance


class StoreOnline(Constraint):
    """A rule for two lists. It declares no valid_types, so it can block the
    upload run and postpone the verify check."""

    def apply(self, task):
        return not self.workflow_config.offline


class ProdOnly(Constraint):
    """An eligibility rule. It reads the environment."""

    valid_types = ConstraintType.ELIGIBILITY

    def apply(self, task):
        return self.env == "prod"


class EnvAllowed(ClassConstraint):
    """An initialization rule. It runs on the class, before an instance
    exists. The class declares its own allowed environments."""

    valid_types = ConstraintType.INITIALIZATION

    def apply(self, task_class, parameters=None):
        return self.env in task_class.allowed_envs


# -- tasks ---------------------------------------------------------------------


class Snapshot(Task):
    """Success comes from the constraint alone. The class overrides no check."""

    success_constraints = [SnapshotPresent]

    def run(self):
        STATE.mkdir(exist_ok=True)
        marker(self).write_text("done\n")


class MarkerTask(Task):
    """Writes one marker file. The check reports it."""

    class Meta:
        abstract = True

    def run(self):
        STATE.mkdir(exist_ok=True)
        marker(self).write_text("done\n")

    def check(self):
        return marker(self).exists()


class Upload(MarkerTask):
    """Two runnability rules in one list. Both must pass before the run."""

    dependencies = Snapshot
    runnability_constraints = [NotInMaintenance, StoreOnline]


class Verify(MarkerTask):
    """A single constraint needs no list."""

    dependencies = Upload
    checkability_constraints = StoreOnline


class Replicate(MarkerTask):
    """Skipped outside production. The instance exists, and the UI shows it."""

    dependencies = Verify
    eligibility_constraints = [ProdOnly]


class Prune(MarkerTask):
    """Dropped outside production. The instance does not exist, and the UI
    does not show it (see Replicate for the visible form)."""

    dependencies = Replicate
    allowed_envs = ("prod",)
    initialization_constraints = [EnvAllowed]
