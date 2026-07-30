from target_base import TargetTask


class Boom(TargetTask):
    """A defect, not a failure: run() raises instead of honestly writing
    nothing, so the runner must label it ERROR rather than FAILED."""

    def run(self):
        raise RuntimeError("boom")


class DependsOnBoom(TargetTask):
    dependencies = Boom


class Innocent(TargetTask):
    """No relation to Boom - whether it completes tells if the batch
    continued past the defect."""


class PartialBoom(TargetTask):
    """The coverup candidate: does its work, then raises. A later check
    honestly finds the artifact - the status must keep the defect visible."""

    def run(self):
        super().run()
        raise RuntimeError("partial boom")


class DependsOnPartial(TargetTask):
    dependencies = PartialBoom
