from target_base import TargetWorkflow


class MyActions(TargetWorkflow):
    """The human-in-the-loop fixture: a task whose check pauses on a missing
    approval marker, and its dependent."""
