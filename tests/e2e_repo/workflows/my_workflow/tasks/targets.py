from winslow import Task

from target_base import TargetTask


class Alpha(TargetTask):
    pass


class Noop(Task):
    """Check-only: no run override, so is_noop is True. The check reads the
    target the way TargetTask does."""

    def check(self):
        return self in self.workflow_config.target


class Beta(TargetTask):
    dependencies = Alpha


class Gamma(TargetTask):
    dependencies = Beta


class Lenient(TargetTask):
    """Passes its check on key existence alone (strict_check = False)."""

    strict_check = False


class FailingCheck(TargetTask):
    """Runs, but never satisfies its success check."""

    def run(self):
        pass


class Blocked(TargetTask):
    """Refuses to run - lands BLOCKED (unless its target is pre-seeded)."""

    def can_run(self):
        self.block("blocked by design")


class Ineligible(TargetTask):
    """Never part of the run - lands SKIPPED."""

    def is_eligible(self):
        return False
