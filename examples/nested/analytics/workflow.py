from winslow import Workflow, Task


class NestedAnalytics(Workflow):
    """A workflow inside the parent directory. It runs on its own, and the parent
    also collects these tasks."""


class Aggregate(Task):
    def check(self):
        return True


class Visualize(Task):
    dependencies = Aggregate

    def check(self):
        return True
