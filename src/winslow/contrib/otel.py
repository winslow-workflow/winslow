"""Error reporting to OpenTelemetry through the telemetry hook.

With an OTLP traces endpoint set, the orchestrator activates
OpenTelemetryConfiguration for each run; subclass it in a telemetry.py
file to customize. A host application that owns its tracer provider calls
setup_opentelemetry instead. Each error becomes one short ERROR span.
Requires the [otel] extra (see docs/telemetry.md).
"""

try:
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode
except ImportError:
    raise ImportError(
        "opentelemetry-api is not installed. "
        "Install the extra: pip install winslow[otel]"
    ) from None

from winslow import settings
from winslow.settings import config
from winslow.exceptions import MisconfigurationError
from winslow.telemetry import (
    TelemetryConfiguration,
    TelemetryHandler,
    register_error_handler,
    get_registered_handler,
)


class OpenTelemetryHandler(TelemetryHandler):
    """Reports each telemetry error as one span with a recorded exception.
    The span is never made current, so it cannot leak into the worker."""

    def __init__(self, tracer_provider=None):
        # With no provider, get_tracer falls back to the global one. In the
        # usual case, the operator configured the global one at startup.
        self._tracer = trace.get_tracer("winslow", tracer_provider=tracer_provider)

    def on_task_error(self, workflow, task, exc, batch_uuid, phase):
        attributes = {
            "winslow.workflow": workflow.instance_name,
            "winslow.workflow_class": type(workflow).__name__,
            # str(workflow): two configured runs differ by it.
            "winslow.workflow_instance": str(workflow),
            "winslow.session_id": workflow.session_id,
            # The declared name, symmetric with winslow.workflow.
            "winslow.task": task.instance_name,
            "winslow.task_class": type(task).__name__,
            # str(task): the declared name plus the parameter values.
            "winslow.task_instance": str(task),
            # A sorted string array; None drops the attribute.
            "winslow.groups": sorted(task.get_groups()) or None,
            "winslow.batch_uuid": batch_uuid,
            "winslow.phase": phase.value,
        }
        # One attribute per parameter and per identifier option, so a backend
        # queries a value directly.
        parameters = {
            f"winslow.parameter.{name}": value
            for name, value in task._parameters_dict_safe.items()
        }
        identifiers = {
            f"winslow.identifier.{name}": value
            for name, value in workflow.identifiers_dict_safe.items()
        }
        self._report("winslow.task_error", exc, attributes | parameters | identifiers)

    def on_unscoped_error(
        self,
        exc,
        workflow_name=None,
        session_id=None,
        workflow_instance=None,
        workflow_class=None,
    ):
        self._report(
            "winslow.unscoped_error",
            exc,
            {
                "winslow.workflow": workflow_name,
                "winslow.workflow_instance": workflow_instance,
                "winslow.workflow_class": workflow_class,
                "winslow.session_id": session_id,
            },
        )

    def _report(self, name, exc, attributes):
        # An attribute with a None value is dropped up front: the OTel API
        # rejects None and would log a warning per span instead.
        attributes = {
            key: value
            for key, value in (attributes | {"winslow.env": settings.env}).items()
            if value is not None
        }
        span = self._tracer.start_span(name, attributes=attributes)
        span.record_exception(exc)
        span.set_status(Status(StatusCode.ERROR, str(exc)))
        span.end()


class OpenTelemetryConfiguration(TelemetryConfiguration):
    """The built-in OpenTelemetry backend: active when an OTLP traces
    endpoint is configured. Override get_tracer_provider or get_handler."""

    def __init__(self):
        self._provider = None

    def get_tracer_provider(self):
        """Build the provider for the configured endpoint, or return None
        to stay inactive."""
        endpoint = config("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", default=None)
        if endpoint is None:
            base = config("OTEL_EXPORTER_OTLP_ENDPOINT", default=None)
            if base is None:
                return None
            endpoint = base.rstrip("/") + "/v1/traces"

        # Import lazily: the handler needs the API only.
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        except ImportError as e:
            raise MisconfigurationError(
                "The environment configures an OTLP endpoint, but the "
                "OpenTelemetry SDK is not installed - install with: "
                "pip install 'winslow[otel]'"
            ) from e

        # shutdown_on_exit is off because shutdown() below owns the flush;
        # the default atexit hook would shut the provider down twice.
        provider = TracerProvider(
            resource=Resource.create(
                {"service.name": config("OTEL_SERVICE_NAME", default="winslow")}
            ),
            shutdown_on_exit=False,
        )
        # Errors are rare: the synchronous processor is fine and cannot lose
        # spans on a fast process exit the way a batch processor would.
        provider.add_span_processor(
            SimpleSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
        )
        return provider

    def get_handler(self, orchestrator_config):
        # A host application may have registered through setup_opentelemetry
        # already; a second handler would export every span twice.
        if get_registered_handler(OpenTelemetryHandler) is not None:
            return None
        self._provider = self.get_tracer_provider()
        if self._provider is None:
            return None
        return OpenTelemetryHandler(tracer_provider=self._provider)

    def shutdown(self):
        if self._provider is not None:
            self._provider.shutdown()


def setup_opentelemetry(tracer_provider=None):
    """Register an OpenTelemetryHandler, for a host application that owns
    its tracer provider. Idempotent: a second call returns the handler
    that is already registered."""
    registered = get_registered_handler(OpenTelemetryHandler)
    if registered is not None:
        return registered
    handler = OpenTelemetryHandler(tracer_provider=tracer_provider)
    register_error_handler(handler)
    return handler
