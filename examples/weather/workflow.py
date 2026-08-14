from pathlib import Path

from winslow import Parameter, Task, Workflow
from winslow.cache import get_workflow_cache

STATE = Path("state")


class Weather(Workflow):
    """Shows both cache scopes: the forecast workflow cache feeds
    get_parameters and the tasks, the stations global cache serves shared
    reference data (see cache.py)."""


class ReportCity(Task):
    """One report per city. The city list comes from the workflow cache before
    a task instance exists, so it is read through the module function."""

    city = Parameter()

    @classmethod
    def get_parameters(cls, workflow_config):
        return [{"city": c} for c in get_workflow_cache().forecast.cities]

    @property
    def report(self):
        return STATE / f"{self.city}.txt"

    def run(self):
        station = self.global_cache.stations.codes[self.city]
        temperature = self.workflow_cache.forecast.conditions[self.city]
        STATE.mkdir(exist_ok=True)
        self.report.write_text(f"{station}: {temperature}c\n")

    def check(self):
        return self.report.exists()


class Summarize(Task):
    """Aggregates the reports after every city wrote one."""

    dependencies = ReportCity

    @property
    def summary(self):
        return STATE / "summary.txt"

    def run(self):
        index = self.workflow_cache.forecast.city_index
        cities = sorted(index, key=index.get)
        reports = (STATE / f"{city}.txt" for city in cities)
        self.summary.write_text("".join(p.read_text() for p in reports))

    def check(self):
        return self.summary.exists()
