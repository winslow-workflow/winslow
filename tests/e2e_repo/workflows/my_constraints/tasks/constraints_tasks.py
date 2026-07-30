from winslow import Task
from winslow.constraints import ClassConstraint, Constraint, ConstraintType

from target_base import TargetTask


# -- constraints ---------------------------------------------------------------
# Not Task subclasses, so task collection ignores them; the tasks below declare
# them and the framework instantiates them with workflow_config.


class Allow(Constraint):
    """Passes any instance gate - unrestricted (empty valid_types), so it
    stands in for eligibility and runnability alike."""

    def apply(self, task):
        return True


class DenyEligibility(Constraint):
    valid_types = {ConstraintType.ELIGIBILITY}

    def apply(self, task):
        return False


class DenyRunnability(Constraint):
    valid_types = {ConstraintType.RUNNABILITY}

    def apply(self, task):
        return False


class DenyCheckability(Constraint):
    valid_types = {ConstraintType.CHECKABILITY}

    def apply(self, task):
        return False


class DenySuccess(Constraint):
    valid_types = {ConstraintType.SUCCESS}

    def apply(self, task):
        return False


class TargetPresent(Constraint):
    """Success purely by constraint: True once run() has written the marker -
    the same world-read TargetTask.check does, as a success constraint."""

    valid_types = {ConstraintType.SUCCESS}

    def apply(self, task):
        return task.target.get(task) is True


class AllowInitialization(ClassConstraint):
    valid_types = {ConstraintType.INITIALIZATION}

    def apply(self, task_class, parameters=None):
        return True


class DenyInitialization(ClassConstraint):
    valid_types = {ConstraintType.INITIALIZATION}

    def apply(self, task_class, parameters=None):
        return False


# -- tasks ---------------------------------------------------------------------


class Completes(TargetTask):
    """All-passing constraints on the gates that have them - lands COMPLETED,
    proving a satisfied constraint never interferes with the run path."""

    eligibility_constraints = [Allow]
    runnability_constraints = [Allow]


class ConstraintSkips(TargetTask):
    """A failing eligibility constraint skips the task before any run
    machinery - the constraint form of is_eligible() -> False."""

    eligibility_constraints = [DenyEligibility]


class OverrideSkips(TargetTask):
    """Constraint passes but the is_eligible() override fails: both are
    consulted (AND), so the task still SKIPs."""

    eligibility_constraints = [Allow]

    def is_eligible(self):
        return False


class ConstraintBlocks(TargetTask):
    """A failing runnability constraint blocks at the runnability gate - the
    constraint form of can_run() -> False."""

    runnability_constraints = [DenyRunnability]


class ConstraintUncheckable(TargetTask):
    """A failing checkability constraint blocks at the completion-check gate,
    before the success check is ever consulted."""

    checkability_constraints = [DenyCheckability]


class ConstraintFailsCheck(TargetTask):
    """A failing success constraint vetoes an otherwise-passing check: the
    task really runs (marker written) yet the constraint-AND-check verdict is
    False, so it lands FAILED."""

    success_constraints = [DenySuccess]


class SuccessByConstraint(Task):
    """Success defined purely by a constraint - no check() override at all.
    run() writes the marker; TargetPresent reads it back."""

    success_constraints = [TargetPresent]

    @property
    def target(self):
        return self.workflow_config.target

    def run(self):
        self.target[self] = True


class Initializes(TargetTask):
    """Passing initialization constraint - present in the graph and runs
    normally, the counterpart to NeverInitialized."""

    initialization_constraints = [AllowInitialization]


class NeverInitialized(TargetTask):
    """A failing initialization constraint drops the task at graph-build - it
    never becomes an instance, so it's absent from the workflow entirely."""

    initialization_constraints = [DenyInitialization]
