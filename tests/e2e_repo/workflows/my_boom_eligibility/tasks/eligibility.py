from target_base import TargetTask


class BoomEligibility(TargetTask):
    """A defect in the eligibility gate itself: the crash is not an answer,
    so the run must abort instead of silently skipping the task."""

    def is_eligible(self):
        raise RuntimeError("boom eligibility")
