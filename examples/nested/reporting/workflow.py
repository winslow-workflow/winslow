from winslow import Workflow, Task


class NestedReporting(Workflow):
    """The second workflow inside the parent directory."""


class Summarize(Task):
    def check(self):
        return True
