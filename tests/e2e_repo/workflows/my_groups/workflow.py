from target_base import TargetWorkflow


class MyGroups(TargetWorkflow):
    """The instance group fixture: a parameterized family gives each instance
    a group of its own, next to a task that keeps the class groups."""
