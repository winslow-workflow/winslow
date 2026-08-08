import sys

from target_base import TargetTask


class ExitsHard(TargetTask):
    """sys.exit in task code is an interpreter abort, not an outcome: the
    runner must convert it to ERROR and keep the batch alive."""

    def run(self):
        sys.exit(3)


class Innocent(TargetTask):
    """No relation to ExitsHard - whether it completes tells if the batch
    survived the sys.exit."""
