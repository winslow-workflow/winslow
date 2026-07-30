from pathlib import Path

from winslow import Workflow, Task

BUILD = Path("build")


class Ci(Workflow):
    """A workflow with task groups, to show the filter expressions. Delete the
    build directory to run it again."""


class MarkerTask(Task):
    """A task that writes one marker file. The check reports the file."""

    class Meta:
        abstract = True

    @property
    def marker_path(self):
        return BUILD / f"{self}.done"

    def run(self):
        BUILD.mkdir(exist_ok=True)
        self.marker_path.write_text("done\n")

    def check(self):
        return self.marker_path.exists()


class Lint(MarkerTask):
    groups = "static"


class Typecheck(MarkerTask):
    groups = "static"


class UnitTest(MarkerTask):
    groups = "tests"
    dependencies = (Lint, Typecheck)


class IntegrationTest(MarkerTask):
    groups = "tests"
    dependencies = UnitTest


class BuildWheel(MarkerTask):
    groups = "release"
    dependencies = IntegrationTest


class PublishWheel(MarkerTask):
    groups = "release"
    dependencies = BuildWheel
