from target_base import TargetTask


class AttrBoom(TargetTask):
    """A typo'd attribute access in run(). Since Python 3.10 the
    AttributeError holds the task on its .obj attribute."""

    def run(self):
        self.attribute_that_does_not_exist
