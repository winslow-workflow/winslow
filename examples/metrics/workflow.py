from pathlib import Path

from winslow import Workflow, Task, ConfigOption, Parameter

STATE = Path("state")


class Metrics(Workflow):
    """Shows parameterization: one task class expands into one instance per
    value. The config options set how many instances exist."""

    days = ConfigOption(type=int, default=3, help_text="The number of days to fetch.")
    regions = ConfigOption(
        choices=["eu", "us", "apac", "latam", "mena"],
        multiselect=True,
        default=["eu", "us"],
        identifier=True,
        help_text="The regions to report on.",
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


def report_regions(cfg):
    return cfg.regions


class FetchDay(MarkerTask):
    """One instance per day. The values come from the workflow config. The self
    dependency with depends_on chains each day to the day before it."""

    day = Parameter(values=lambda cfg: list(range(1, cfg.days + 1)))
    dependencies = "self"

    def depends_on(self, task):
        return task.day == self.day - 1


class RegionReport(MarkerTask):
    """One instance per region, from the regions option. A class dependency
    reaches every instance of the class, so each report depends on every
    fetched day."""

    region = Parameter(values=report_regions)
    dependencies = FetchDay


class Publish(MarkerTask):
    """One instance per region. The depends_on filter pairs each publish with
    the report of its own region."""

    region = Parameter(values=report_regions)
    dependencies = RegionReport

    def depends_on(self, task):
        return task.region == self.region
