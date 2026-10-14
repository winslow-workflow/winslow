# Telemetry

Winslow can report every error of a run to an external backend, together with the context an
operator needs to act on it: the workflow, the session, the task and its parameters, the batch and
the execution phase.

Two backends ship with Winslow, [Sentry](#sentry) and [OpenTelemetry](#opentelemetry), and both
follow the same five steps: install the extra, configure the environment, run the workflow,
observe the errors in the backend, and customize by subclassing when the defaults are not enough.
There is no bootstrap code at any step. This page walks both backends through these steps,
explains [how activation works](#how-activation-works) underneath, and ends with the seam for
[writing your own backend](#write-a-custom-backend).

## What is reported

A backend receives defects, and only defects:

- an exception that escapes a step of a task (the task status becomes `ERROR`),
- `sys.exit()` inside task code, and a task signal that no ladder consumes,
- a workflow that does not start (`EligibilityError`, `InitializationError`),
- a session that fails outside a batch.

Expected outcomes stay out: a failed check and a task signal (skip, block, fail, action required)
are verdicts, not defects.

## Set up a failing task

Every example on this page reports the same defect: a parameterized task that raises in `run()`.
To follow along, add it to the [etl workflow](workflows.md):

```python title="workflows/etl/tasks/boom.py"
from winslow import Parameter, Task


class Boom(Task):
    region = Parameter(values=("eu", "us"))

    def run(self):
        raise RuntimeError(f"boom in {self.region}")

    def check(self):
        return False  # The work is never done, so run() always executes.
```

## Sentry

The Sentry backend turns each error into one Sentry event.

### Install the extra

=== "uv"

    ```bash
    uv add "winslow[sentry]"
    ```

=== "pip"

    ```bash
    pip install 'winslow[sentry]'
    ```

### Configure the environment

List the built-in `SentryConfiguration` in `TELEMETRY` and set `SENTRY_DSN`, both in the
[settings module](settings.md). The configuration activates for each run and each serve, and flushes
its events before the process exits (see [How activation works](#how-activation-works)).

```python title="etl/settings.py"
from decouple import config

TELEMETRY = ["winslow.contrib.sentry.SentryConfiguration"]
SENTRY_DSN = config("SENTRY_DSN")
```

| Setting              | Purpose                                        | When None                  |
| -------------------- | ---------------------------------------------- | -------------------------- |
| `SENTRY_DSN`         | Selects the project.                           | Required, unless a host initialized the SDK. |
| `SENTRY_ENVIRONMENT` | The `environment` tag of each event.           | Falls back to `ENV`. |
| `SENTRY_RELEASE`     | The `release` of each event.                   | The SDK detects it (git, CI values). |

### Run the workflow

```bash
export SENTRY_DSN="https://examplePublicKey@o0.ingest.sentry.io/0"
winslow run --mode headless --workflow etl
```

### Observe the events

The run reports one event per parameter row of `Boom`: the Sentry project shows two new issues,
`boom (eu)` and `boom (us)`. Each event carries these tags to search and filter on (the
environment arrives through the native SDK field, not a tag):

| Tag                 | Value                                              | Example              |
| ------------------- | -------------------------------------------------- | -------------------- |
| `workflow`          | The declared workflow name.                        | `etl`                |
| `workflow_class`    | The workflow class name.                           | `Etl`                |
| `workflow_instance` | `str(workflow)`: the name plus the [identifier options](workflows.md#the-identifier-options) of the run. | `etl (client=acme)` |
| `task`              | The declared task name.                            | `boom`               |
| `task_class`        | The task class name.                               | `Boom`               |
| `task_instance`     | `str(task)`: the name plus the parameter values.   | `boom (eu)`          |
| `groups`            | The groups of the task, comma-joined; absent without groups. | `mild`     |
| `session_id`        | The id of the execution session.                   | `etl-20260808T…`     |
| `batch_uuid`        | The id of the batch, one run or check action.      | `39b83ad0-…`         |
| `phase`             | The execution phase of the errored step.           | `run`                |

Beyond the tags, each event carries the parameter values and the identifier options as extra
data, and the run log records that led to the error as breadcrumbs.

Sentry folds events into one issue when their fingerprints match. Winslow fingerprints an error
with the workflow instance, the task instance and the exception type: `boom (eu)` and `boom (us)`
become two separate issues, and so do two runs of the workflow with different `--client` values.
The grouping is this fine because such a failure is usually a config or data error of one specific
combination, and one issue per task class would hide which combination fails.

A workflow that does not start reports the same way, with the workflow and session tags only.

### Customize: report from production only

The backend reports wherever it is listed. To report from production only, list it in the settings
module of production and not in the one of a laptop:

```python title="etl/settings.py"
from decouple import config

ENV = config("WINSLOW_ENV", default="local")
TELEMETRY = ["winslow.contrib.sentry.SentryConfiguration"] if ENV == "prod" else []
```

Run the workflow again: no events arrive, because `ENV` is `local`. With `WINSLOW_ENV=prod` in the
environment the module lists the backend, and the events flow as before.

## OpenTelemetry

The OpenTelemetry backend turns each error into one short span with the exception recorded on it.

### Install the extra

=== "uv"

    ```bash
    uv add "winslow[otel]"
    ```

=== "pip"

    ```bash
    pip install 'winslow[otel]'
    ```

### Configure the endpoint

List the built-in `OpenTelemetryConfiguration` in `TELEMETRY` and set `OTEL_TRACES_ENDPOINT`, the
full URL of the traces endpoint, both in the [settings module](settings.md). The configuration
activates for each run and each serve, and flushes its spans before the process exits (see
[How activation works](#how-activation-works)).

```python title="etl/settings.py"
TELEMETRY = ["winslow.contrib.otel.OpenTelemetryConfiguration"]
OTEL_TRACES_ENDPOINT = "http://localhost:4318/v1/traces"
```

### Run the workflow

```bash
winslow run --mode headless --workflow etl
```

### Observe the spans

The run exports one span per errored `Boom` instance: the span is named `winslow.task_error`, its
status is `ERROR`, and the exception is recorded on it as the standard exception event. A workflow
that does not start exports the same shape under the name `winslow.unscoped_error`.

Look for the spans under the `SERVICE_NAME` of the deployment, `winslow` by default.
`OTEL_SERVICE_NAME` overrides it for the tracer alone. The
service identifies the winslow process, not a workflow: one process can run many workflows, and
the spans of all of them share its service name. To select one workflow, filter on the span
attributes:

| Attribute                    | Value                                              | Example              |
| ---------------------------- | -------------------------------------------------- | -------------------- |
| `winslow.workflow`           | The declared workflow name.                        | `etl`                |
| `winslow.workflow_class`     | The workflow class name.                           | `Etl`                |
| `winslow.workflow_instance`  | `str(workflow)`: the name plus the identifier options of the run. | `etl (client=acme)` |
| `winslow.task`               | The declared task name.                            | `boom`               |
| `winslow.task_class`         | The task class name.                               | `Boom`               |
| `winslow.task_instance`      | `str(task)`: the name plus the parameter values.   | `boom (eu)`          |
| `winslow.parameter.<name>`   | One attribute per parameter of the task.           | `winslow.parameter.region: eu` |
| `winslow.identifier.<name>`  | One attribute per identifier option of the run.    | `winslow.identifier.client: acme` |
| `winslow.groups`             | The groups of the task; absent without groups.     | `["mild"]`           |
| `winslow.session_id`         | The id of the execution session.                   | `etl-20260808T…`     |
| `winslow.batch_uuid`         | The id of the batch, one run or check action.      | `39b83ad0-…`         |
| `winslow.phase`              | The execution phase of the errored step.           | `run`                |
| `winslow.env`                | The `ENV` setting.                                 | `prod`               |

### Customize: replace the tracer provider

The built-in builds its tracer provider from the settings alone. To change the exporter, the
resource or the processor, subclass `OpenTelemetryConfiguration` in a module of the project, override
`get_tracer_provider`, and list the subclass in `TELEMETRY` in place of the built-in. This example
replaces the service name and stamps the owning team onto the resource, so every span carries both:

```python title="etl/telemetry.py"
from decouple import config
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from winslow.contrib.otel import OpenTelemetryConfiguration


class TaggedTelemetry(OpenTelemetryConfiguration):
    """Stamps the service name and the owning team onto every span."""

    def get_tracer_provider(self):
        endpoint = config("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", default=None)
        if endpoint is None:
            return None  # stay inactive without an endpoint

        # shutdown_on_exit stays off: the configuration owns the flush, and
        # the default atexit hook would shut the provider down twice.
        provider = TracerProvider(
            resource=Resource.create(
                {"service.name": "data-pipelines", "team": "data-platform"}
            ),
            shutdown_on_exit=False,
        )
        provider.add_span_processor(
            SimpleSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
        )
        return provider
```

Run the workflow again: every span now carries the `team` resource attribute. For a change beyond
the provider, other span names or other attributes, override `get_handler` instead.

## How activation works

The `TELEMETRY` setting lists the configuration classes of a process, each as a dotted path. At the
start of a run or a serve, Winslow imports each class in list order, builds it, and registers the
handler it answers. At exit it flushes the backends in reverse order and unregisters the handlers,
so an embedding process can start again. A `show` runs no telemetry.

A listed configuration is wanted: it builds at every start, with its extra installed and its
settings set. An unlisted built-in costs nothing.

An active backend is safe for the run itself: a backend that fails while reporting is logged and
never reaches the batch.

## Write a custom backend

The built-ins cover Sentry and OpenTelemetry, but the seam behind them is open. To add a backend
next to the built-ins, declare a subclass of `TelemetryConfiguration` in a module of the project and
list it in `TELEMETRY`: the configuration builds the handler, and the handler consumes the errors.

```python title="etl/telemetry.py"
from winslow.telemetry import TelemetryConfiguration, TelemetryHandler


class PagerHandler(TelemetryHandler):
    def on_task_error(self, workflow, task, exc, batch_uuid, phase):
        ...  # page the on-call

    def on_unscoped_error(self, exc, **context):
        ...


class PagerTelemetry(TelemetryConfiguration):
    def get_handler(self, orchestrator_config):
        return PagerHandler()
```

```python title="etl/settings.py"
TELEMETRY = ["etl.telemetry.PagerTelemetry"]
```

One rule binds a handler: it runs on the worker thread that hit the error, so it must not block.

A host application that embeds winslow and owns its process does not need the configuration seam:
it registers a handler directly with `winslow.telemetry.register_error_handler`, or calls the setup
functions of the contrib modules.
