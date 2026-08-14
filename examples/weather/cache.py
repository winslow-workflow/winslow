import time

from winslow.cache import (
    GlobalCache,
    JsonFileStorage,
    MemoryStorage,
    WorkflowCache,
    compose,
    entry,
)


# --8<-- [start:declare]
class Forecast(WorkflowCache):
    """Session-scope data: a new session builds a fresh instance. cities feeds
    get_parameters, conditions expires after its ttl and recomputes."""

    @entry(eager=True)
    def cities(self):
        self.logger.info("forecast: listing the cities (1s)…")
        time.sleep(1)  # stands in for a slow catalog call
        return ("oslo", "bergen", "tromso")

    @entry(eager=True, depends_on="cities")
    def city_index(self):
        return {city: index for index, city in enumerate(self.cities)}

    @entry(ttl=300)
    def conditions(self):
        self.logger.info("forecast: sampling the conditions (1s)…")
        time.sleep(1)  # stands in for a slow sensor sweep
        return {"oslo": -3, "bergen": 4, "tromso": -8}


class Stations(GlobalCache):
    """Process-scope reference data, shared by every workflow."""

    @entry(eager=True)
    def codes(self):
        self.logger.info("stations: loading the station registry (2s)…")
        time.sleep(2)  # stands in for a slow reference-data query
        return {"oslo": "ENOS", "bergen": "ENBG", "tromso": "ENTR"}

    # --8<-- [end:declare]
    # The file tier keeps the registry warm: a second run reads
    # .winslow/cache/ and skips the slow load.
    storage_class = compose(MemoryStorage, JsonFileStorage)
