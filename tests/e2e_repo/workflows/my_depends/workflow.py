from target_base import TargetWorkflow


class MyDepends(TargetWorkflow):
    """The depends_on fixture: a producer family fans out per number, and
    consumers narrow their class-level dependency to matching instances."""
