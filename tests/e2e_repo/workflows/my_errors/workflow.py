from target_base import TargetWorkflow


class MyErrors(TargetWorkflow):
    """The ERROR fixture: a task whose run() raises, its dependent, and a
    bystander proving the batch survives a defect elsewhere."""
