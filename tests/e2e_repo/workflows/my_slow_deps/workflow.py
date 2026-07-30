from target_base import TargetWorkflow


class MySlowDeps(TargetWorkflow):
    """The concurrent-dependency fixture: gated producers a consumer's
    dependency resolution must wait out across batches - one that lands its
    work, one that fails its check."""
