from target_base import TargetWorkflow


class MyCache(TargetWorkflow):
    """The cache fixture: a workflow cache feeds get_parameters, the tasks
    read and invalidate entries."""
