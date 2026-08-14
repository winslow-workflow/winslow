from winslow.cache import WorkflowCache, entry


class WeatherCache(WorkflowCache):
    """Session-scope fixture: eager entries with a dependency, plus a lazy entry
    in the invalidation cascade. The journal records each loader call."""

    name = "weather"

    def __init__(self, workflow_config):
        super().__init__(workflow_config)
        self.loads = []

    @entry(eager=True)
    def cities(self):
        self.loads.append("cities")
        return ("athens", "bergen", "cairo")

    @entry(eager=True, depends_on="cities")
    def city_index(self):
        # The journal write comes after the read: the cities lock then orders
        # the two entries in `loads`, under any population schedule.
        index = {c: i for i, c in enumerate(self.cities)}
        self.loads.append("city_index")
        return index

    @entry(depends_on="cities")
    def forecast(self):
        self.loads.append("forecast")
        return tuple(c.upper() for c in self.cities)
