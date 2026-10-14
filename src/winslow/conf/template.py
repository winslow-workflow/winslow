"""A settings module to copy, as settings.py beside the workflows directory,
or named with WINSLOW_SETTINGS_MODULE=etl.settings. Every name here is a setting
winslow reads, with its default. Drop the lines you keep at the default. The
secrets read the environment through decouple, which also reads a .env file
beside this module. Add the names your own backends read, and name winslow
code as strings."""

from decouple import config

# The environment name of the process and the header color per name.
ENV = config("WINSLOW_ENV", default="local")
SERVICE_NAME = "winslow"
ENV_COLORS = {
    "prod": "red",
    "uat": "gold",
    "staging": "orange",
    "dev": "green",
    "local": "royalblue",
}

# The code the process wires, as dotted paths.
AUTH_BACKENDS = ["winslow.access.backends.TokenBackend"]
STATE_STORE = "winslow.state.FileStateStore"
TELEMETRY = []
TUI_PLUGINS = []
FILTER_PLUGINS = []

# The serve credentials. The ticket backend needs both of these.
AUTH_TICKET_SECRET = config("WINSLOW_TICKET_SECRET", default=None)
SERVE_PUBLIC_URL = config("WINSLOW_PUBLIC_URL", default=None)
SERVE_ORIGINS = ()
AUTH_MAX_CONNECTION = 3600
AUTH_OPEN = False

# The files of the two file stores and the directories, relative to the CWD.
AUTH_POLICY_FILE = ".winslow/policy.toml"
AUTH_TOKENS_FILE = ".winslow/tokens"
STATE_DIR = ".winslow/state"
CACHE_DIR = ".winslow/cache"
LOG_DIR = ".winslow/logs"

# The client side: None is the XDG config path.
CREDENTIALS_FILE = None

# Logging.
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S UTC"
LOG_UTC = True
LOG_JSON = False
TASK_LOG_BUFFER_SIZE = 1000
EXECUTION_RECORD_LOG_BUFFER_SIZE = 1000
CACHE_SNAPSHOT_SIZE_BYTES = 32 * 1024

# The telemetry backends, read when TELEMETRY lists one.
SENTRY_DSN = config("SENTRY_DSN", default=None)
SENTRY_ENVIRONMENT = config("SENTRY_ENVIRONMENT", default=None)
SENTRY_RELEASE = config("SENTRY_RELEASE", default=None)
OTEL_TRACES_ENDPOINT = config("OTEL_TRACES_ENDPOINT", default=None)
OTEL_SERVICE_NAME = config("OTEL_SERVICE_NAME", default=None)

# The OpenID Connect backend, read when AUTH_BACKENDS lists it.
OIDC_ISSUER = config("OIDC_ISSUER", default=None)
OIDC_CLIENT_IDS = ()
OIDC_ALLOWED_DOMAINS = ()
OIDC_CLIENT_ID = config("OIDC_CLIENT_ID", default=None)
OIDC_CLIENT_SECRET = config("OIDC_CLIENT_SECRET", default=None)
