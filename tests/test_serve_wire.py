"""Pure winslow.serve.wire function tests that need no live session or
websocket: the payload builders take a cache/workflow/session object
directly and return a plain dict."""

import time
from argparse import Namespace

from winslow.cache import WorkflowCache, entry
from winslow.serve.wire import cache_card_payload


class FlakyCache(WorkflowCache):
    """A ttl'd entry that succeeds once, then fails every recompute - the
    shape of a value that goes stale and cannot refresh."""

    def __init__(self, workflow_config):
        super().__init__(workflow_config)
        self.calls = 0

    @entry(ttl=0.01)
    def value(self):
        self.calls += 1
        if self.calls == 1:
            return "first value"
        raise RuntimeError("boom on recompute")


def flaky_cache():
    return FlakyCache(Namespace(cache_namespace="test"))


def test_cache_card_omits_the_preview_of_an_errored_entry():
    cache = flaky_cache()
    cache.value  # populates with "first value"
    time.sleep(0.02)  # past the ttl
    try:
        cache.value  # recompute fails, leaves the old value quarantined
    except RuntimeError:
        pass

    card = cache_card_payload(cache)
    (info,) = [i for i in card["info"] if i["entry_name"] == "value"]
    assert info["state"] == "errored"
    assert "value" not in card["values"]


def test_cache_card_previews_a_warm_entry():
    cache = flaky_cache()
    cache.value

    card = cache_card_payload(cache)
    assert card["values"]["value"] == "first value"
