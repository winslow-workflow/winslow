"""The project-level cache location: GlobalCache subclasses live here."""

from winslow.cache import GlobalCache, entry


class MyGlobalCache(GlobalCache):
    """Process-scope fixture: one eager and one lazy entry, with a per-instance
    load journal the tests read."""

    def __init__(self, orchestrator_config):
        super().__init__(orchestrator_config)
        self.loads = []

    @entry(eager=True)
    def stations(self):
        self.loads.append("stations")
        return ("north", "south")

    @entry
    def climates(self):
        self.loads.append("climates")
        return {"north": "polar"}
