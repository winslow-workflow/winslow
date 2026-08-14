from winslow import Parameter
from winslow.cache import get_workflow_cache

from target_base import TargetTask


class LoadCities(TargetTask):
    """Parameterized from the cache: get_parameters runs before the task
    instances exist, so it reads the container through the context variable."""

    city = Parameter()

    @classmethod
    def get_parameters(cls, workflow_config):
        return [{"city": c} for c in get_workflow_cache().weather.cities]

    def run(self):
        index = self.workflow_cache.weather.city_index
        self.target[("loaded", self.city)] = index[self.city]
        super().run()


class RefreshForecast(TargetTask):
    """Reads the lazy entry, then invalidates its upstream. The invalidation
    log line must land in the log view of this task."""

    def run(self):
        weather = self.workflow_cache.weather
        self.target[("forecast",)] = weather.forecast
        weather.invalidate("cities")
        super().run()
