# Settings

Winslow reads its settings from one Python module, named by one environment variable. The module
holds every value a deployment pins and names the code it wires: the auth backends, the stores,
the telemetry backends and the plugins. A laptop runs with no module and the defaults.

## The settings module

A `settings.py` beside the `workflows/` directory is the module, picked up from the working directory
the way the workflows are:

```bash
cd etl
winslow serve --host 0.0.0.0        # reads ./settings.py
```

`WINSLOW_SETTINGS_MODULE` names another module instead. The value is an import path, so the module
lives in a package the process can import:

```bash
export WINSLOW_SETTINGS_MODULE=etl.settings
winslow serve --host 0.0.0.0
```

The module is plain Python. Every upper-case name is a setting. A name winslow defines replaces the
default, and a name winslow does not define is kept for the backends, stores and plugins of the
project to read. The module names code as dotted strings:

```python title="etl/settings.py"
from decouple import config

ENV = config("WINSLOW_ENV", default="local")
LOG_JSON = True

AUTH_BACKENDS = [
    "winslow.access.backends.TokenBackend",
    "winslow.access.backends.TicketBackend",
]
AUTH_TICKET_SECRET = config("WINSLOW_TICKET_SECRET")
SERVE_PUBLIC_URL = "https://etl-host"
SERVE_ORIGINS = ("https://etl.example",)

TELEMETRY = ["winslow.contrib.sentry.SentryConfiguration"]
SENTRY_DSN = config("SENTRY_DSN")

TUI_PLUGINS = ["etl.ui.ReviewPane"]
```

The environment is the module's concern. The example reads its secrets through
[python-decouple](https://pypi.org/project/python-decouple/), which also reads a `.env` file beside the
module. Winslow itself reads one variable, `WINSLOW_SETTINGS_MODULE`.

`winslow.conf.template` is a module to copy: every setting with its default, the secrets through
decouple. Copy it into the project, drop the lines that keep the default, and name it.

## The settings

The code the process wires, each a dotted path or a list of them:

| setting | default | what it names |
|---|---|---|
| `AUTH_BACKENDS` | the token backend | the credential kinds a server accepts ([serve](serve.md#credentials)) |
| `STATE_STORE` | `"winslow.state.FileStateStore"` | the store of the session state (see [Sessions](sessions.md)) |
| `TELEMETRY` | `[]` | the telemetry backends, in order (see [Telemetry](telemetry.md)) |
| `TUI_PLUGINS` | `[]` | the UI plugins of the terminal (see [UI plugins](ui-plugins.md)) |
| `FILTER_PLUGINS` | `[]` | the filter plugins of the engine (see [Filter plugins](filter-plugins.md)) |

A listed class is wanted: it loads at every start, with the settings it reads.

The values:

| setting | default | holds |
|---|---|---|
| `ENV` | `"local"` | the environment name, `self.env` on a task and a workflow |
| `SERVICE_NAME` | `"winslow"` | the name of the deployment, in the MCP server name and the headers |
| `ENV_COLORS` | [the color table](serve.md#the-environment-color) | the header color per environment name |
| `STATE_DIR`, `CACHE_DIR`, `LOG_DIR` | under `.winslow/` | the directories, relative to the working directory |
| `LOG_FORMAT`, `LOG_DATEFMT`, `LOG_UTC` | the timestamped format, UTC | the console and session log lines |
| `LOG_JSON` | `False` | one JSON object per record on stdout (see [Logs](serve.md#logs)) |
| `TASK_LOG_BUFFER_SIZE`, `EXECUTION_RECORD_LOG_BUFFER_SIZE` | `1000` | the lines kept per task and per record |
| `CACHE_SNAPSHOT_SIZE_BYTES` | `32768` | the byte cap of one rendered cache read in history |
| `AUTH_POLICY_FILE`, `AUTH_TOKENS_FILE` | `.winslow/policy.toml`, `.winslow/tokens` | the policy and the tokens |
| `AUTH_TICKET_SECRET`, `SERVE_PUBLIC_URL` | `None` | the ticket secret and the audience of every ticket |
| `SERVE_ORIGINS` | `()` | the browser origins the websocket endpoint accepts |
| `AUTH_MAX_CONNECTION` | `3600` | the seconds a credential holds a socket |
| `AUTH_OPEN` | `False` | the development switch: every connection passes with every permission |
| `CREDENTIALS_FILE` | `None`, the XDG config path | on the client: the credential file of the person |
| `SENTRY_DSN`, `SENTRY_ENVIRONMENT`, `SENTRY_RELEASE` | `None` | the Sentry backend |
| `OTEL_TRACES_ENDPOINT` | `None` | the OpenTelemetry backend |
| `OTEL_SERVICE_NAME` | `None` | the service of the traces, `SERVICE_NAME` by default |
| `OIDC_ISSUER` | `None` | the [issuer](serve.md#an-openid-connect-issuer) |
| `OIDC_CLIENT_IDS`, `OIDC_ALLOWED_DOMAINS` | `()`, `()` | the allow-lists of the issuer |
| `OIDC_CLIENT_ID` | `None` | the client `auth login --kind oidc` logs in with |
| `OIDC_CLIENT_SECRET` | `None` | its secret, for an issuer that demands one of an installed app |

A value has the type of its default: `LOG_UTC` takes a bool, `SERVE_ORIGINS` a tuple.

## Read a setting

Project code reads the same object winslow reads:

```python
from winslow.conf import import_string, settings

settings.ENV
settings.OKTA_ISSUER                    # a name the project module added
import_string(settings.STATE_STORE)     # the class STATE_STORE names
```

A path setting holds its dotted string. `import_string` resolves one where the code needs the class.

## Check the wiring

`winslow settings` prints every setting with its value and its source, `default` or `module`, then
every path setting with the class it resolved to. A secret prints as `set` or `unset`:

```
$ winslow settings
settings module: etl.settings
AUTH_MAX_CONNECTION = 3600  [default]
ENV = 'prod'  [module]
AUTH_TICKET_SECRET = set  [module]
...

STATE_STORE -> winslow.state.FileStateStore
AUTH_BACKENDS -> winslow.access.backends.TokenBackend, winslow.access.backends.TicketBackend
TELEMETRY -> winslow.contrib.sentry.SentryConfiguration
```

The command runs before the workflow collection, so it works in a directory with no workflows.
