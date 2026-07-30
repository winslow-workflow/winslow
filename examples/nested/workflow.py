from winslow import Workflow, Task


class Nested(Workflow):
    """The parent workflow. It owns two tasks, and it also collects the tasks of
    the two workflows in the subdirectories."""


class Ingest(Task):
    def check(self):
        return True


class Transform(Task):
    dependencies = Ingest

    def check(self):
        return True
