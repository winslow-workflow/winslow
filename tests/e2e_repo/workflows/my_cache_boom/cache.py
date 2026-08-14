from winslow.cache import WorkflowCache, entry


class BoomCache(WorkflowCache):
    """The cache.py file form of the location rule, with a failing eager
    loader."""

    @entry(eager=True)
    def kaboom(self):
        raise RuntimeError("boom cache")
