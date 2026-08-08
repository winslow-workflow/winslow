# Telemetry

Winslow can report every error of a run to an external backend, together with the context an
operator needs to act on it: the workflow, the session, the task and its parameters, the batch and
the execution phase.

Two backends ship with Winslow, [Sentry](#sentry) and [OpenTelemetry](#opentelemetry), and both
follow the same model: they activate implicitly when their environment values are present, and a
repo changes their behavior by subclassing their configuration class. This page explains what is
reported, walks through both backends, and ends with the seam for
[writing your own](#write-a-custom-backend).

## What is reported

A backend receives defects, and only defects:

- an exception that escapes a step of a task (the task status becomes `ERROR`),
- `sys.exit()` inside task code, and a task signal that no ladder consumes,
- a workflow that does not start (`EligibilityError`, `InitializationError`),
- a session that fails outside a batch.

Expected outcomes stay out: a failed check and a task signal (skip, block, fail, action required)
are verdicts, not defects.

## Activation

Both backends activate configuration first. When the environment carries their values, a DSN for
Sentry or an OTLP endpoint for OpenTelemetry, the backend activates on its own for each run: no
class, no import, no bootstrap code. Without the values, the backend stays inactive and adds no
cost.

An active backend is safe for the run itself: a backend that fails while reporting is logged and
never reaches the batch.

When the defaults are not enough, the repo declares a subclass of the built-in configuration class
in a `telemetry.py` file. Winslow collects these files the way it collects `workflow.py` files, and
among the collected classes only the most derived one activates. A subclass therefore replaces the
built-in it inherits from, and never doubles the reports.

!!! info "A configured backend needs its extra installed"

    When the environment activates a backend whose extra is not installed, the run does not start
    (`MisconfigurationError`).

## The running example

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

The Sentry backend turns each error into one Sentry event. Install the extra first:

```bash
pip install 'winslow[sentry]'
```

### Implicit activation

Setting `SENTRY_DSN` is the whole integration: with the DSN present, the built-in
`SentryConfiguration` activates for each run and flushes its events before the process exits.

| Environment value    | Purpose                                        | When unset                 |
| -------------------- | ---------------------------------------------- | -------------------------- |
| `SENTRY_DSN`         | Activates the backend and selects the project. | The backend stays inactive. |
| `SENTRY_ENVIRONMENT` | The `environment` tag of each event.           | Falls back to `WINSLOW_ENV`. |
| `SENTRY_RELEASE`     | The `release` of each event.                   | The SDK detects it (git, CI values). |

The values are read through python-decouple: the search for a `.env` file starts in the working
directory and walks up its parents, and an exported variable always wins over the file.

```bash
export SENTRY_DSN="https://examplePublicKey@o0.ingest.sentry.io/0"
winslow run --mode headless --workflow etl
```

The run above reports one event per parameter row of `Boom`. Each event carries these tags to
search and filter on (the environment arrives through the native SDK field, not a tag):

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

### Override by subclassing

The built-in activates wherever the DSN is set. To decide differently, subclass
`SentryConfiguration` in a `telemetry.py` file and override `get_handler`: the subclass replaces
the built-in, and nothing else changes. This example reports from production only:

```python title="telemetry.py"
from winslow import settings
from winslow.contrib.sentry import SentryConfiguration


class ProdOnlySentry(SentryConfiguration):
    """Reports from production only, also where a DSN is set."""

    def get_handler(self, orchestrator_config):
        if settings.env != "prod":
            return None
        return super().get_handler(orchestrator_config)
```

Run the workflow again: no events arrive, because `WINSLOW_ENV` defaults to `dev`. With
`WINSLOW_ENV=prod` the events flow as before.

## OpenTelemetry

The OpenTelemetry backend turns each error into one short span with the exception recorded on it.
Install the extra first:

```bash
pip install 'winslow[otel]'
```

### Implicit activation

Setting an OTLP traces endpoint is the whole integration: with an endpoint present, the built-in
`OpenTelemetryConfiguration` activates for each run and flushes its spans before the process exits.
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` is used exactly as given; the more general
`OTEL_EXPORTER_OTLP_ENDPOINT` gets `/v1/traces` appended, following the OTLP http convention. Both
are read through python-decouple, like the Sentry values.

```bash
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="http://localhost:4318/v1/traces"
winslow run --mode headless --workflow etl
```

The run above exports one span per errored `Boom` instance: the span is named
`winslow.task_error`, its status is `ERROR`, and the exception is recorded on it as the standard
exception event. A workflow that does not start exports the same shape under the name
`winslow.unscoped_error`.

The spans arrive under the service name `winslow` (set `OTEL_SERVICE_NAME` to change it). The
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
| `winslow.env`                | `WINSLOW_ENV`.                                     | `prod`               |

### Override by subclassing

The built-in builds its tracer provider from the environment alone. To change the exporter, the
resource or the processor, subclass `OpenTelemetryConfiguration` and override
`get_tracer_provider`. This example replaces the service name and stamps the owning team onto the
resource, so every span carries both:

```python title="telemetry.py"
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

## Write a custom backend

The built-ins cover Sentry and OpenTelemetry, but the seam behind them is open. A direct subclass
of `TelemetryConfiguration` adds a backend next to the built-ins: the configuration decides when
its backend is active and builds the handler, and the handler consumes the errors.

```python title="telemetry.py"
from winslow import settings
from winslow.telemetry import TelemetryConfiguration, TelemetryHandler


class PagerHandler(TelemetryHandler):
    def on_task_error(self, workflow, task, exc, batch_uuid, phase):
        ...  # page the on-call

    def on_unscoped_error(self, exc, **context):
        ...


class PagerTelemetry(TelemetryConfiguration):
    def get_handler(self, orchestrator_config):
        if settings.env != "prod":
            return None  # page from production only
        return PagerHandler()
```

One rule binds a handler: it runs on the worker thread that hit the error, so it must not block.

A host application that embeds winslow and owns its process does not need the configuration seam:
it registers a handler directly with `winslow.telemetry.register_error_handler`, or calls the setup
functions of the contrib modules.
