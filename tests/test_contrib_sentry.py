import logging

import pytest

sentry_sdk = pytest.importorskip("sentry_sdk")

from winslow import settings
from winslow.contrib.sentry import (
    SentryConfiguration,
    SentryTelemetryHandler,
    setup_sentry,
)
from winslow.exceptions import EligibilityError
from winslow.runner.execution import ExecutionPhase
from winslow.telemetry import get_registered_handlers, unregister_error_handler

from harness import build_workflow, by_name, by_params, headless_orchestrator, run_all


TEST_DSN = "https://key@sentry.invalid/1"


class CapturingTransport(sentry_sdk.transport.Transport):
    """An in-memory transport: each captured event lands in `captured` as a
    plain dict, and nothing touches the network."""

    def __init__(self, captured):
        super().__init__()
        self.captured = captured

    def capture_envelope(self, envelope):
        event = envelope.get_event()
        if event is not None:
            self.captured.append(event)


@pytest.fixture
def events():
    captured = []
    sentry_sdk.init(
        dsn=TEST_DSN,
        transport=CapturingTransport(captured),
        default_integrations=False,
    )
    yield captured
    # close() flushes but leaves the client attached and is_active() True -
    # only detaching it from the global scope resets the SDK for the next test.
    sentry_sdk.get_client().close()
    sentry_sdk.get_global_scope().set_client(None)


@pytest.fixture
def handler(events):
    # The init above made a client active, so this also exercises the
    # host-owns-the-client path: setup_sentry must only register.
    handler = setup_sentry()
    yield handler
    unregister_error_handler(handler)


def test_task_error_event(e2e_repo, mode, events, handler):
    """One event per errored task, with the tags, the extra display form and
    the (workflow instance, task instance, exception type) fingerprint."""
    workflow = build_workflow(e2e_repo, "my-errors", mode)
    tasks = by_name(workflow)

    run_all(workflow)

    by_task = {event["tags"]["task_class"]: event for event in events}
    assert set(by_task) == {"Boom", "PartialBoom"}
    boom = by_task["Boom"]
    assert boom["tags"]["workflow"] == "my-errors"
    assert boom["tags"]["workflow_class"] == "MyErrors"
    # The declared name, symmetric with the workflow tag.
    assert boom["tags"]["task"] == tasks["Boom"].instance_name
    # Boom declares no groups, so the tag stays absent instead of empty.
    assert "groups" not in boom["tags"]
    assert boom["tags"]["phase"] == "run"
    assert boom["tags"]["batch_uuid"]
    # The environment is the native SDK field, set at init - a custom env
    # tag would show the same value twice in the issue view.
    assert "env" not in boom["tags"]
    assert boom["tags"]["task_instance"] == str(tasks["Boom"])
    assert boom["fingerprint"] == ["my-errors", str(tasks["Boom"]), "RuntimeError"]
    assert boom["exception"]["values"][-1]["type"] == "RuntimeError"
    # Boom is not parameterized, so no parameters extra is attached.
    assert "parameters" not in boom.get("extra", {})


def test_reraise_still_one_event(e2e_repo, mode, events, handler):
    """--reraise-errors lets the exception escape to the caller, but the
    capture happens once, at the boundary."""
    workflow = build_workflow(
        e2e_repo, "my-errors", mode, "--reraise-errors", "--disable-concurrency"
    )

    with pytest.raises(RuntimeError, match="boom"):
        run_all(workflow)

    assert len(events) == 1


def test_unscoped_error_event(e2e_repo, events):
    """A workflow that never starts reports with the workflow and the session
    stamped, and without a task tag - there is no task. No handler is
    registered here: the active client alone activates the built-in
    SentryConfiguration for the run, the configuration-first path end to
    end."""
    orchestrator = headless_orchestrator(e2e_repo, "my-boom-eligibility")

    with pytest.raises(EligibilityError):
        orchestrator.start()

    (event,) = events
    assert event["tags"]["workflow"] == "my-boom-eligibility"
    assert event["tags"]["session_id"]
    assert "task" not in event["tags"]
    assert event["fingerprint"] == ["my-boom-eligibility", "EligibilityError"]
    # The run is over: the auto-activated handler must be gone.
    assert not any(
        isinstance(h, SentryTelemetryHandler) for h in get_registered_handlers()
    )


def test_parameters_reach_the_extra_data(e2e_repo, mode, events):
    """The parameter values of a parameterized task reach the event as extra
    data, keyed by parameter name."""
    workflow = build_workflow(e2e_repo, "my-params", mode)
    task = by_params(workflow)[("Deploy", "eu")]

    SentryTelemetryHandler().on_task_error(
        workflow, task, RuntimeError("boom"), "b-1", ExecutionPhase.RUN
    )

    (event,) = events
    assert event["extra"]["parameters"] == {"region": "eu"}


def test_parameter_rows_get_their_own_issue(e2e_repo, mode, events):
    """Each parameter row fingerprints as its own Sentry issue: such
    failures are config or data errors of one row, so the rows fail
    independently and one issue per class would hide which row is sick."""
    workflow = build_workflow(e2e_repo, "my-params", mode)
    tasks = by_params(workflow)
    error = RuntimeError("boom")
    handler = SentryTelemetryHandler()

    handler.on_task_error(
        workflow, tasks[("Deploy", "eu")], error, "b-1", ExecutionPhase.RUN
    )
    handler.on_task_error(
        workflow, tasks[("Deploy", "us")], error, "b-1", ExecutionPhase.RUN
    )

    eu_event, us_event = events
    assert eu_event["fingerprint"] != us_event["fingerprint"]
    assert eu_event["fingerprint"] == ["my-params", "deploy (eu)", "RuntimeError"]
    assert us_event["fingerprint"] == ["my-params", "deploy (us)", "RuntimeError"]
    # The class stays searchable as a tag across the per-row issues.
    assert eu_event["tags"]["task_class"] == us_event["tags"]["task_class"] == "Deploy"


def test_workflow_instance_in_tags_and_fingerprint(e2e_repo, events, handler):
    """Two configured runs of one workflow must not share an issue: the
    fingerprint uses str(workflow), which carries the identifier options,
    and the tag makes the instance searchable."""
    orchestrator = headless_orchestrator(e2e_repo, "my-identified", "--client", "acme")

    orchestrator.start()

    (event,) = events
    assert event["tags"]["workflow"] == "my-identified"
    assert event["tags"]["workflow_instance"] == "my-identified (client=acme)"
    assert event["tags"]["task_instance"] == "boom"
    assert event["extra"]["identifiers"] == {"client": "acme"}
    assert event["fingerprint"] == [
        "my-identified (client=acme)",
        "boom",
        "RuntimeError",
    ]


def test_groups_and_declared_name_tags(e2e_repo, mode, events):
    """A grouped task carries its groups as one sorted, comma-joined tag,
    and its declared name as its own tag next to the class name."""
    workflow = build_workflow(e2e_repo, "my-filters", mode)
    task = by_name(workflow)["Sweet"]

    SentryTelemetryHandler().on_task_error(
        workflow, task, RuntimeError("boom"), "b-1", ExecutionPhase.RUN
    )

    (event,) = events
    assert event["tags"]["groups"] == "mild"
    assert event["tags"]["task_class"] == "Sweet"
    assert event["tags"]["task"] == "sweet"


def test_configuration_inactive_without_dsn_or_client():
    """The gate of the built-in: no DSN and no active client means None -
    the backend costs nothing where it is not configured."""
    sentry_sdk.get_global_scope().set_client(None)

    assert SentryConfiguration().get_handler(object()) is None


def test_configuration_activates_from_the_environment(events, monkeypatch):
    """SENTRY_DSN alone activates the built-in: it owns the SDK init (PII
    off, WINSLOW_ENV as the environment) and returns the handler."""
    sentry_sdk.get_client().close()
    sentry_sdk.get_global_scope().set_client(None)
    monkeypatch.setenv("SENTRY_DSN", TEST_DSN)
    configuration = SentryConfiguration()

    handler = configuration.get_handler(object())

    assert isinstance(handler, SentryTelemetryHandler)
    client = sentry_sdk.get_client()
    assert client.is_active()
    assert client.options["send_default_pii"] is False
    assert client.options["environment"] == settings.env
    configuration.shutdown()  # flush must leave the client active
    assert sentry_sdk.get_client().is_active()


def test_setup_initializes_when_no_client(events):
    """Without an active client, setup_sentry owns the init: PII off, and the
    environment defaults to WINSLOW_ENV instead of the SDK's "production"."""
    sentry_sdk.get_client().close()
    sentry_sdk.get_global_scope().set_client(None)

    handler = setup_sentry(dsn=TEST_DSN, transport=CapturingTransport(events))
    try:
        client = sentry_sdk.get_client()
        assert client.is_active()
        assert client.options["send_default_pii"] is False
        assert client.options["environment"] == settings.env
    finally:
        unregister_error_handler(handler)


def test_setup_is_idempotent(events):
    """Autodiscovery can execute a workflow module more than once per
    process, so a second setup call must return the registered handler
    instead of doubling every report."""
    first = setup_sentry()
    second = setup_sentry()
    try:
        assert second is first
        registered = [
            h
            for h in get_registered_handlers()
            if isinstance(h, SentryTelemetryHandler)
        ]
        assert registered == [first]
    finally:
        unregister_error_handler(first)


def test_error_logs_become_breadcrumbs_not_events(events):
    """The runner logs each errored task at ERROR level next to the emit -
    with the SDK default LoggingIntegration, every error would report twice.
    The setup keeps log records as breadcrumbs, and the task boundary stays
    the single event source."""
    sentry_sdk.get_client().close()
    sentry_sdk.get_global_scope().set_client(None)
    handler = setup_sentry(dsn=TEST_DSN, transport=CapturingTransport(events))
    try:
        logging.getLogger("winslow").error("task boom - marked errored")
        assert events == []

        handler.on_unscoped_error(RuntimeError("boom"), workflow_name="wf")
    finally:
        unregister_error_handler(handler)

    (event,) = events
    messages = [crumb["message"] for crumb in event["breadcrumbs"]["values"]]
    assert "task boom - marked errored" in messages


def test_setup_honors_sentry_environment_and_release(events, monkeypatch):
    """SENTRY_ENVIRONMENT and SENTRY_RELEASE pass to the SDK explicitly:
    the SDK reads os.environ only, so a .env value would otherwise be lost."""
    monkeypatch.setenv("SENTRY_ENVIRONMENT", "staging")
    monkeypatch.setenv("SENTRY_RELEASE", "1.2.3")
    sentry_sdk.get_client().close()
    sentry_sdk.get_global_scope().set_client(None)

    handler = setup_sentry(dsn=TEST_DSN, transport=CapturingTransport(events))
    try:
        client = sentry_sdk.get_client()
        assert client.options["environment"] == "staging"
        assert client.options["release"] == "1.2.3"
    finally:
        unregister_error_handler(handler)


def test_setup_keeps_the_active_client(events):
    """A host application that owns sentry_sdk.init keeps its client: a
    second init would silently replace it."""
    client = sentry_sdk.get_client()

    handler = setup_sentry(dsn="https://other@sentry.invalid/2")
    try:
        assert sentry_sdk.get_client() is client
    finally:
        unregister_error_handler(handler)
