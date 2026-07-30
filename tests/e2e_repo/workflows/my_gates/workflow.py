from target_base import TargetWorkflow


class MyGates(TargetWorkflow):
    """The session-lifecycle fixture: a task that holds its batch open until
    the test releases it, plus followers for the stop-sweep scenarios."""
