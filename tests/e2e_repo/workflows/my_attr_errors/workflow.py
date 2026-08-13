from target_base import TargetWorkflow


class MyAttrErrors(TargetWorkflow):
    """The exception-attribute fixture: a run() whose error carries the task
    on an attribute (AttributeError.obj, see ExecutionBatch.release_error)."""
