from target_base import TargetWorkflow


class MyWorkflow(TargetWorkflow):
    """The outcome-spectrum fixture: a dependency chain plus one task per
    non-passing outcome (failed check, blocked, skipped)."""
