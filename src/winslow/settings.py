"""Process-wide run settings sourced from the environment (python-decouple)."""

import os

from decouple import AutoConfig

config = AutoConfig(search_path=os.getcwd())

env = config("WINSLOW_ENV", default="dev")

# The live buffer of a task and the log mirror of the execution record hold the
# same stream for that task. Each has its own limit, so the memory of History
# stays bounded.
_DEFAULT_LOG_BUFFER_SIZE = 1000
TASK_LOG_BUFFER_SIZE = config(
    "WINSLOW_TASK_LOG_BUFFER_SIZE", default=_DEFAULT_LOG_BUFFER_SIZE, cast=int
)
EXECUTION_RECORD_LOG_BUFFER_SIZE = config(
    "WINSLOW_EXECUTION_RECORD_LOG_BUFFER_SIZE",
    default=_DEFAULT_LOG_BUFFER_SIZE,
    cast=int,
)

# The byte cap of one rendered cache read in history. A cache class overrides
# it with snapshot_size_bytes (see BaseCache).
CACHE_SNAPSHOT_SIZE_BYTES = config(
    "WINSLOW_CACHE_SNAPSHOT_SIZE_BYTES", default=32 * 1024, cast=int
)
