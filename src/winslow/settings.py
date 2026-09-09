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

# The directories are relative to the CWD.
STATE_DIR = config("WINSLOW_STATE_DIR", default=".winslow/state")
CACHE_DIR = config("WINSLOW_CACHE_DIR", default=".winslow/cache")
LOG_DIR = config("WINSLOW_LOG_DIR", default=".winslow/logs")
STATE_BACKEND = config("WINSLOW_STATE_BACKEND", default="file")

LOG_FORMAT = config(
    "WINSLOW_LOG_FORMAT", default="%(asctime)s - %(levelname)s - %(message)s"
)
LOG_DATEFMT = config("WINSLOW_LOG_DATEFMT", default="%Y-%m-%d %H:%M:%S UTC")
LOG_UTC = config("WINSLOW_LOG_UTC", default=True, cast=bool)
# One JSON object per console record, for a pod whose log store queries
# fields (see StructuredFormatter). The default stays human-readable.
LOG_JSON = config("WINSLOW_LOG_JSON", default=False, cast=bool)

# The serve credentials (see winslow.serve.auth). SERVE_ORIGINS is a comma
# separated list of the browser origins the websocket endpoint accepts.
SERVE_TOKEN = config("WINSLOW_TOKEN", default=None)
SERVE_TICKET_SECRET = config("WINSLOW_TICKET_SECRET", default=None)
SERVE_ORIGINS = config("WINSLOW_ORIGINS", default="")
