from pathlib import Path

from winslow import Workflow, Task, ConfigOption

STATE = Path("state")


class Delivery(Workflow):
    """Shows the dependency forms: a class, a name, a group and depends_on. The
    legacy flag makes the skipped-task case observable."""

    legacy = ConfigOption(
        action="store_true", default=False, help_text="Make the legacy ingest task eligible."
    )


class MarkerTask(Task):
    """Writes one marker file. The check reports it."""

    class Meta:
        abstract = True

    @property
    def marker(self):
        return STATE / f"{self}.done"

    def run(self):
        STATE.mkdir(exist_ok=True)
        self.marker.write_text("done\n")

    def check(self):
        return self.marker.exists()


# The ingest group. Three tasks share the "ingest" label.
class IngestOrders(MarkerTask):
    groups = "ingest"


class IngestPayments(MarkerTask):
    groups = "ingest"


class IngestLegacy(MarkerTask):
    groups = "ingest"

    def is_eligible(self):
        return self.workflow_config.legacy  # Skipped unless --legacy is passed.


# A group dependency: every task in the ingest group.
class Validate(MarkerTask):
    dependencies = "ingest"


# A name dependency: one specific task, by its name.
class Reconcile(MarkerTask):
    dependencies = "ingest-orders"


# A class dependency.
class Transform(MarkerTask):
    dependencies = Validate


# The checks group. Only the critical check is a real dependency of Deploy.
class SecurityScan(MarkerTask):
    groups = "checks"
    critical = True


class StyleCheck(MarkerTask):
    groups = "checks"
    critical = False


# Two classes and a group. depends_on keeps only the critical members of checks.
class Deploy(MarkerTask):
    dependencies = (Transform, Reconcile, "checks")

    def depends_on(self, task):
        # Depend on a check only when it is critical. Depend on every other task.
        return getattr(task, "critical", True)
