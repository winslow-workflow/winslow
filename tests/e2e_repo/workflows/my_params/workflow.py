from target_base import TargetWorkflow


class MyParams(TargetWorkflow):
    """The parameterization fixture: task classes fan out into multiple
    instances, so lookups must key on (class name, parameter values)."""
