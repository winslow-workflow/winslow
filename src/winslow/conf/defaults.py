"""The base of the settings: every setting winslow reads, once, with its
default, as a plain constant. A project module overrides these names and adds
its own (see winslow.conf.load). No default reads the environment."""

# The environment name of the process and the header color per name, a CSS
# color name or a hex value. Every client of one server shows the same color
# (see Settings.env_color).
ENV = "local"
# The name of this deployment: the MCP server is `<SERVICE_NAME>-<ENV>`, the
# header of every client shows both, and the tracer takes it as its service.
SERVICE_NAME = "winslow"
ENV_COLORS = {
    "prod": "red",
    "uat": "gold",
    "staging": "orange",
    "dev": "green",
    "local": "royalblue",
}

# The live buffer of a task and the log mirror of the execution record hold the
# same stream for that task. Each has its own limit, so the memory of History
# stays bounded.
TASK_LOG_BUFFER_SIZE = 1000
EXECUTION_RECORD_LOG_BUFFER_SIZE = 1000

# The byte cap of one rendered cache read in history. A cache class overrides
# it with snapshot_size_bytes (see BaseCache).
CACHE_SNAPSHOT_SIZE_BYTES = 32 * 1024

# The directories are relative to the CWD. {env} resolves to ENV, so each
# environment keeps its own sessions, caches and logs (see winslow.conf.load).
STATE_DIR = ".winslow/{env}/state"
CACHE_DIR = ".winslow/{env}/cache"
LOG_DIR = ".winslow/{env}/logs"

LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S UTC"
LOG_UTC = True
# One JSON object per console record, for a pod whose log store queries
# fields (see StructuredFormatter). The default stays human-readable.
LOG_JSON = False

# The credential file of the person, one entry per server (see
# winslow.access.credentials). None is the XDG config path, computed at use.
CREDENTIALS_FILE = None

# The doors of a serve process. SERVE_PUBLIC_URL is the URL clients reach the
# server on, behind the proxy that ends TLS: the audience of a ticket and of an
# OIDC token, and an accepted origin. SERVE_ORIGINS holds the browser origins
# the websocket endpoint accepts.
SERVE_PUBLIC_URL = None
SERVE_ORIGINS = ()
# The secret that signs the tickets (see winslow.access.tickets).
AUTH_TICKET_SECRET = None
# The socket bound in seconds: the websocket door caps every context at now
# plus this (see Authenticator.verify_hello).
AUTH_MAX_CONNECTION = 3600
# The development switch: every connection passes on any bind with every permission
# (see Authenticator.verify_hello).
AUTH_OPEN = False

# The code the process wires, as dotted paths (see winslow.conf.import_string).
AUTH_BACKENDS = ["winslow.access.backends.TokenBackend"]
STATE_STORE = "winslow.state.FileStateStore"
TELEMETRY = []
TUI_PLUGINS = []
FILTER_PLUGINS = []

# The policy file and the token file of the default backends, relative to the CWD.
AUTH_POLICY_FILE = ".winslow/policy.toml"
AUTH_TOKENS_FILE = ".winslow/tokens"

# The telemetry backends read these (see winslow.contrib).
SENTRY_DSN = None
SENTRY_ENVIRONMENT = None
SENTRY_RELEASE = None
OTEL_TRACES_ENDPOINT = None
OTEL_SERVICE_NAME = None

# The OpenID Connect backend (see winslow.contrib.oidc): the issuer, the
# clients it registered for this server, the login domains it admits, the
# client `winslow auth login --kind oidc` logs in with, and its secret when
# the issuer demands one of an installed app (Google).
OIDC_ISSUER = None
OIDC_CLIENT_IDS = ()
OIDC_ALLOWED_DOMAINS = ()
OIDC_CLIENT_ID = None
OIDC_CLIENT_SECRET = None
