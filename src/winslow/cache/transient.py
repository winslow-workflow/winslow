from contextlib import contextmanager


# batch_uuid -> {task -> {transient name: value}}
_TRANSIENT_CACHE: dict = {}


def phase_cache(task, batch_uuid) -> dict:
    return _TRANSIENT_CACHE.setdefault(batch_uuid, {}).setdefault(task, {})


def peek_phase_cache(task, batch_uuid) -> dict:
    return _TRANSIENT_CACHE.get(batch_uuid, {}).get(task, {})


def reset_phase_cache(task, batch_uuid):
    _TRANSIENT_CACHE.setdefault(batch_uuid, {})[task] = {}


def drop_batch_cache(batch_uuid):
    _TRANSIENT_CACHE.pop(batch_uuid, None)


@contextmanager
def batch_cache(batch_uuid):
    """Scope the transient cache of a batch. The cache is dropped at the exit of
    the block, for each type of exit."""
    try:
        yield batch_uuid
    finally:
        drop_batch_cache(batch_uuid)
