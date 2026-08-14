from target_base import TargetWorkflow


class MyCacheBoom(TargetWorkflow):
    """A workflow whose eager cache entry raises: the initialization must
    abort loudly."""
