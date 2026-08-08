import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode

from winslow import settings
from winslow.contrib.otel import (
    OpenTelemetryConfiguration,
    OpenTelemetryHandler,
    setup_opentelemetry,
)
from winslow.runner.execution import ExecutionPhase
from winslow.exceptions import EligibilityError
from winslow.telemetry import get_registered_handlers, unregister_error_handler

from harness import build_workflow, by_name, by_params, headless_orchestrator, run_all


@pytest.fixture
def spans():
    """A private tracer provider on an in-memory exporter: the handler gets
    it directly, so the global provider of the process stays untouched."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    handler = setup_opentelemetry(tracer_provider=provider)
    yield exporter
    unregister_error_handler(handler)
    provider.shutdown()


def test_task_error_span(e2e_repo, mode, spans):
    """One ERROR span per errored task, with the exception recorded as the
    standard OTel exception event and the run identity as attributes."""
    workflow = build_workflow(e2e_repo, "my-errors", mode)
    tasks = by_name(workflow)

    run_all(workflow)

    by_task = {
        span.attributes["winslow.task_class"]: span
        for span in spans.get_finished_spans()
    }
    assert set(by_task) == {"Boom", "PartialBoom"}
    boom = by_task["Boom"]
    assert boom.name == "winslow.task_error"
    assert boom.status.status_code is StatusCode.ERROR
    assert boom.attributes["winslow.workflow"] == "my-errors"
    assert boom.attributes["winslow.workflow_class"] == "MyErrors"
    # The declared name, symmetric with winslow.workflow.
    assert boom.attributes["winslow.task"] == tasks["Boom"].instance_name
    assert boom.attributes["winslow.task_instance"] == str(tasks["Boom"])
    # Boom declares no groups, so the attribute stays absent instead of empty.
    assert "winslow.groups" not in boom.attributes
    assert boom.attributes["winslow.phase"] == "run"
    assert boom.attributes["winslow.batch_uuid"]
    assert boom.attributes["winslow.env"] == settings.env
    (event,) = boom.events
    assert event.name == "exception"
    assert event.attributes["exception.type"] == "RuntimeError"


def test_groups_and_declared_name_attributes(e2e_repo, mode):
    """A grouped task carries its groups as a string-array attribute - the
    idiomatic OTel shape for a multi-valued label - and its declared name
    as its own attribute next to the class name."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    handler = OpenTelemetryHandler(tracer_provider=provider)
    workflow = build_workflow(e2e_repo, "my-filters", mode)
    task = by_name(workflow)["Sweet"]

    handler.on_task_error(
        workflow, task, RuntimeError("boom"), "b-1", ExecutionPhase.RUN
    )

    (span,) = exporter.get_finished_spans()
    assert span.attributes["winslow.groups"] == ("mild",)
    assert span.attributes["winslow.task_class"] == "Sweet"
    assert span.attributes["winslow.task"] == "sweet"
    provider.shutdown()


def test_parameters_become_attributes(e2e_repo, mode):
    """One attribute per parameter, prefixed winslow.parameter, so a backend
    queries a value directly instead of substring-matching the display."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    handler = OpenTelemetryHandler(tracer_provider=provider)
    workflow = build_workflow(e2e_repo, "my-params", mode)
    task = by_params(workflow)[("Deploy", "eu")]

    handler.on_task_error(
        workflow, task, RuntimeError("boom"), "b-1", ExecutionPhase.RUN
    )

    (span,) = exporter.get_finished_spans()
    assert span.attributes["winslow.parameter.region"] == "eu"
    provider.shutdown()


def test_configuration_inactive_without_endpoint():
    """The gate of the built-in: no OTLP endpoint means None - the backend
    costs nothing where it is not configured."""
    assert OpenTelemetryConfiguration().get_handler(object()) is None


def test_configuration_activates_from_the_environment(monkeypatch):
    """An OTLP endpoint alone activates the built-in: the generic endpoint
    gets /v1/traces appended (the OTLP http convention), the service name
    defaults to winslow, and shutdown flushes the provider."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid:4318")
    configuration = OpenTelemetryConfiguration()

    handler = configuration.get_handler(object())

    assert isinstance(handler, OpenTelemetryHandler)
    provider = configuration._provider
    assert provider.resource.attributes["service.name"] == "winslow"
    (processor,) = provider._active_span_processor._span_processors
    assert (
        processor.span_exporter._endpoint == "http://collector.invalid:4318/v1/traces"
    )
    configuration.shutdown()


def test_configuration_uses_the_traces_endpoint_as_given(monkeypatch):
    """OTEL_EXPORTER_OTLP_TRACES_ENDPOINT names the full path already and
    wins over the generic endpoint."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://generic.invalid:4318")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://traces.invalid:4318/v1/traces"
    )
    monkeypatch.setenv("OTEL_SERVICE_NAME", "my-service")
    configuration = OpenTelemetryConfiguration()

    handler = configuration.get_handler(object())

    assert handler is not None
    provider = configuration._provider
    assert provider.resource.attributes["service.name"] == "my-service"
    (processor,) = provider._active_span_processor._span_processors
    assert processor.span_exporter._endpoint == "http://traces.invalid:4318/v1/traces"
    configuration.shutdown()


def test_setup_is_idempotent():
    """Autodiscovery can execute a workflow module more than once per
    process, so a second setup call must return the registered handler
    instead of doubling every span."""
    first = setup_opentelemetry()
    second = setup_opentelemetry()
    try:
        assert second is first
        registered = [
            h for h in get_registered_handlers() if isinstance(h, OpenTelemetryHandler)
        ]
        assert registered == [first]
    finally:
        unregister_error_handler(first)


def test_workflow_instance_attribute(e2e_repo, spans):
    """A configured run stamps its identifier onto every span, so a backend
    can tell two runs of one workflow apart."""
    orchestrator = headless_orchestrator(e2e_repo, "my-identified", "--client", "acme")

    orchestrator.start()

    (span,) = spans.get_finished_spans()
    assert span.attributes["winslow.workflow_instance"] == "my-identified (client=acme)"
    assert span.attributes["winslow.workflow"] == "my-identified"
    assert span.attributes["winslow.identifier.client"] == "acme"


def test_unscoped_error_span(e2e_repo, spans):
    """A workflow that never starts reports a span with the workflow and the
    session attributes, and without task attributes - there is no task."""
    orchestrator = headless_orchestrator(e2e_repo, "my-boom-eligibility")

    with pytest.raises(EligibilityError):
        orchestrator.start()

    (span,) = spans.get_finished_spans()
    assert span.name == "winslow.unscoped_error"
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["winslow.workflow"] == "my-boom-eligibility"
    assert span.attributes["winslow.session_id"]
    assert "winslow.task" not in span.attributes
    (event,) = span.events
    # record_exception qualifies a non-builtin type with its module.
    assert event.attributes["exception.type"] == "winslow.exceptions.EligibilityError"
