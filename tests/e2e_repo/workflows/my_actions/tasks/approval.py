from target_base import TargetTask


class NeedsApproval(TargetTask):
    """run() alone can't finish the job: the check demands a sign-off marker
    that only a human (in tests: the test itself) ever writes."""

    def check(self):
        if self not in self.target:
            return False
        if ("approved", self) not in self.target:
            self.require_action("approval marker missing - sign off to proceed")
        return True


class DependsOnApproval(TargetTask):
    dependencies = NeedsApproval
