from pathlib import Path

from winslow import Workflow, Task, ConfigOption

STATE = Path("state")
ARTIFACT = STATE / "artifact"
REPORT = STATE / "report"
PUBLISHED = STATE / "published"


class Release(Workflow):
    """A small release flow. It shows the task gates next to the run and check
    methods. Delete the state directory to run it again."""

    maintenance = ConfigOption(
        action="store_true", default=False, help_text="Block the publish task."
    )
    offline = ConfigOption(
        action="store_true", default=False, help_text="Block the verify check."
    )


class Build(Task):
    def run(self):
        STATE.mkdir(exist_ok=True)
        ARTIFACT.write_text("built\n")

    def check(self):
        return ARTIFACT.exists()


class Test(Task):
    dependencies = Build

    def run(self):
        REPORT.write_text("tests passed\n")

    def check(self):
        return REPORT.exists()


class Verify(Task):
    """A check-only task. It has no run method, so it only reports a state. The
    can_check gate postpones the check when the service is offline."""

    dependencies = Test

    def can_check(self):
        self.block_if(self.workflow_config.offline, "the service is offline")
        return True

    def check(self):
        return REPORT.exists()


class Publish(Task):
    """Runs only in the production environment. The can_run gate blocks it during
    a maintenance window."""

    dependencies = Verify

    def is_eligible(self):
        return self.env == "prod"

    def can_run(self):
        self.block_if(self.workflow_config.maintenance, "a maintenance window is open")
        return True

    def run(self):
        PUBLISHED.write_text("published\n")

    def check(self):
        return PUBLISHED.exists()
