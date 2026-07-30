from target_base import TargetTask


class BoomCheck(TargetTask):
    """The check itself is the defect, before the run ever starts: with no
    trustworthy answer to "am I already done?", the run path must not be
    entered at all. Named to sort first, so the reraise leg aborts on it."""

    def check(self):
        raise RuntimeError("boom check")


class ChokesOnArtifact(TargetTask):
    """The post-run counterpart: honest while there is nothing, but the
    artifact its own run lands is exactly what breaks the check. A
    ("calm", task) target marker repairs the check without re-running -
    the redemption candidate for the check-side COMPLETED_WITH_ERROR."""

    def check(self):
        if self in self.target and ("calm", self) not in self.target:
            raise RuntimeError("chokes on artifact")
        return super().check()


class DependsOnBoomCheck(TargetTask):
    dependencies = BoomCheck


class Innocent(TargetTask):
    """No relation to the broken checks - whether it completes tells if the
    batch continued past the defects."""


class SkipsMidCheck(TargetTask):
    """Signal misuse: skip is eligibility's verb, raised here mid-check where
    no ladder consumes it - it must land ERROR, never SKIPPED. Named to sort
    last so the reraise test's abort-on-BoomCheck ordering holds."""

    def check(self):
        self.skip("trying to skip mid-check")
