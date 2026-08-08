import json
from collections import Counter

import pytest

from winslow.exceptions import EligibilityError, MisconfigurationError, TaskSkip
from winslow.runner.execution import ExecutionPhase
from winslow.session import Session
from winslow.task.status import TaskStatus as S
from winslow.telemetry import (
    TelemetryConfiguration,
    TelemetryHandler,
    activate_telemetry_configurations,
    get_registered_handlers,
    register_error_handler,
    shutdown_telemetry_configurations,
    unregister_error_handler,
)

from harness import build_workflow, by_name, headless_orchestrator, run_all


class RecordingHandler(TelemetryHandler):
    def __init__(self):
        self.task_errors = []
        self.unscoped_errors = []

    def on_task_error(self, workflow, task, exc, batch_uuid, phase):
        self.task_errors.append((task, exc, batch_uuid, phase))

    def on_unscoped_error(
        self,
        exc,
        workflow_name=None,
        session_id=None,
        workflow_instance=None,
        workflow_class=None,
    ):
        self.unscoped_errors.append(
            (exc, workflow_name, session_id, workflow_instance, workflow_class)
        )


class ExplodingHandler(TelemetryHandler):
    """The hostile consumer: every callback raises. The contract says the
    batch must never notice."""

    def on_task_error(self, workflow, task, exc, batch_uuid, phase):
        raise ValueError("handler boom")

    def on_unscoped_error(self, exc, workflow_name=None, session_id=None, **kwargs):
        raise ValueError("handler boom")


@pytest.fixture
def telemetry():
    handler = RecordingHandler()
    register_error_handler(handler)
    yield handler
    unregister_error_handler(handler)


def test_one_emit_per_errored_task(e2e_repo, mode, telemetry):
    """A defect in run() reaches the hook exactly once, with the batch and
    the phase attached - and nothing else in the batch emits: the blocked
    dependent, the completed bystander and every FAILED pre-run check are
    outcomes, not defects."""
    workflow = build_workflow(e2e_repo, "my-errors", mode)
    tasks = by_name(workflow)

    run_all(workflow)

    emitted = {
        task: (exc, batch_uuid, phase)
        for task, exc, batch_uuid, phase in telemetry.task_errors
    }
    assert len(telemetry.task_errors) == 2
    assert set(emitted) == {tasks["Boom"], tasks["PartialBoom"]}
    for exc, batch_uuid, phase in emitted.values():
        assert isinstance(exc, RuntimeError)
        assert batch_uuid is not None
        assert phase is ExecutionPhase.RUN
    assert telemetry.unscoped_errors == []


def test_sys_exit_emits(e2e_repo, mode, telemetry):
    """sys.exit escapes as a BaseException, outside the Exception branch -
    the SystemExit handler must report it like any other defect, and the
    batch must survive it."""
    workflow = build_workflow(e2e_repo, "my-telemetry", mode)
    tasks = by_name(workflow)

    run_all(workflow)

    ((task, exc, batch_uuid, phase),) = telemetry.task_errors
    assert task is tasks["ExitsHard"]
    assert isinstance(exc, SystemExit)
    assert batch_uuid is not None
    assert phase is ExecutionPhase.RUN
    assert workflow.store[tasks["Innocent"]] is S.COMPLETED


def test_expected_outcomes_do_not_emit(e2e_repo, mode, telemetry):
    """Signals are flow control and FAILED is an honest verdict: skip, block,
    a failing check, an unsatisfied checkability gate and action-required all
    stay out of the hook."""
    run_all(build_workflow(e2e_repo, "my-workflow", mode))
    run_all(build_workflow(e2e_repo, "my-constraints", mode))
    run_all(build_workflow(e2e_repo, "my-actions", mode))

    assert telemetry.task_errors == []
    assert telemetry.unscoped_errors == []


def test_check_defects_and_illegal_signals_emit(e2e_repo, mode, telemetry):
    """A crash in check() and a signal raised where no ladder consumes it are
    defects. One emit per errored step: BoomCheck errors twice, once in its
    own pre-run check and once in the dependency probe by DependsOnBoomCheck."""
    workflow = build_workflow(e2e_repo, "my-check-errors", mode)
    tasks = by_name(workflow)

    run_all(workflow)

    emitted = Counter(
        (task, type(exc), phase) for task, exc, _, phase in telemetry.task_errors
    )
    assert emitted == Counter(
        [
            (tasks["BoomCheck"], RuntimeError, ExecutionPhase.PRE_RUN_CHECK),
            (tasks["BoomCheck"], RuntimeError, ExecutionPhase.CHECK),
            (tasks["SkipsMidCheck"], TaskSkip, ExecutionPhase.PRE_RUN_CHECK),
            (tasks["ChokesOnArtifact"], RuntimeError, ExecutionPhase.POST_RUN_CHECK),
        ]
    )


def test_handler_exception_never_reaches_the_batch(e2e_repo, mode):
    """A handler that raises is logged and swallowed: the batch outcomes are
    identical to a run without any handler."""
    handler = ExplodingHandler()
    register_error_handler(handler)
    try:
        workflow = build_workflow(e2e_repo, "my-errors", mode)
        tasks = by_name(workflow)
        run_all(workflow)
    finally:
        unregister_error_handler(handler)

    # The same terminal statuses that test_error_is_contained pins down.
    assert workflow.store[tasks["Boom"]] is S.FAILED
    assert workflow.store[tasks["Innocent"]] is S.COMPLETED
    assert workflow.store[tasks["PartialBoom"]] is S.COMPLETED_WITH_ERROR


def test_reraise_still_emits_at_the_boundary(e2e_repo, mode, telemetry):
    """--reraise-errors lets the exception escape to the caller, but the
    report already happened at the boundary - once, before the raise."""
    workflow = build_workflow(
        e2e_repo, "my-errors", mode, "--reraise-errors", "--disable-concurrency"
    )
    tasks = by_name(workflow)

    with pytest.raises(RuntimeError, match="boom"):
        run_all(workflow)

    assert [task for task, *_ in telemetry.task_errors] == [tasks["Boom"]]


def test_eligibility_error_emits_unscoped_error(e2e_repo, telemetry):
    """A crash in is_eligible aborts the run before any batch - invisible on
    a headless box unless the hook reports it, stamped with the workflow and
    the session that never got to work."""
    orchestrator = headless_orchestrator(e2e_repo, "my-boom-eligibility")

    with pytest.raises(EligibilityError):
        orchestrator.start()

    ((exc, workflow_name, session_id, workflow_instance, workflow_class),) = (
        telemetry.unscoped_errors
    )
    assert isinstance(exc, EligibilityError)
    assert workflow_name == "my-boom-eligibility"
    assert session_id is not None
    # No identifier options declared: the instance is the bare name.
    assert workflow_instance == "my-boom-eligibility"
    assert workflow_class == "MyBoomEligibility"
    assert telemetry.task_errors == []


def test_misconfiguration_does_not_emit(e2e_repo, telemetry):
    """Bad input gets a clean CLI message, not an error report - an unknown
    workflow name is the operator's typo, not a defect."""
    orchestrator = headless_orchestrator(e2e_repo, "no-such-workflow")

    with pytest.raises(MisconfigurationError):
        orchestrator.start()

    assert telemetry.unscoped_errors == []
    assert telemetry.task_errors == []


def test_mark_error_emits_unscoped_error(e2e_repo, mode, telemetry):
    """The session-failed path of the interactive app: mark_error carries the
    exception into the hook with the session identity attached."""
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    session = Session(workflow)
    error = RuntimeError("init failed")

    session.mark_error(error)

    assert telemetry.unscoped_errors == [
        (
            error,
            workflow.instance_name,
            session.session_id,
            str(workflow),
            type(workflow).__name__,
        )
    ]


def test_workflow_instance_reaches_the_context(e2e_repo):
    """A workflow with identifier options stamps its display form onto the
    log context, so a backend can tell two configured runs apart the way
    str(task) tells two parameter rows apart."""
    instances = []

    class ContextHandler(TelemetryHandler):
        def on_task_error(self, workflow, task, exc, batch_uuid, phase):
            from winslow.task.context import get_log_context

            instances.append(get_log_context().workflow_instance)

    handler = ContextHandler()
    register_error_handler(handler)
    try:
        orchestrator = headless_orchestrator(
            e2e_repo, "my-identified", "--client", "acme"
        )
        orchestrator.start()
    finally:
        unregister_error_handler(handler)

    assert instances == ["my-identified (client=acme)"]


class _ListHandler(TelemetryHandler):
    pass


def test_activation_registers_and_shutdown_unregisters():
    """The framework-owned lifecycle: get_handler receives the orchestrator
    config, an active handler is registered for the run, a None gate stays
    out, and shutdown unregisters and flushes."""
    sentinel = object()

    class Active(TelemetryConfiguration):
        def get_handler(self, orchestrator_config):
            self.seen_config = orchestrator_config
            return _ListHandler()

        def shutdown(self):
            self.flushed = True

    class GatedOff(TelemetryConfiguration):
        def get_handler(self, orchestrator_config):
            return None

    active = activate_telemetry_configurations([Active, GatedOff], sentinel)

    ((configuration, handler),) = active
    assert type(configuration) is Active
    assert configuration.seen_config is sentinel
    assert handler in get_registered_handlers()

    shutdown_telemetry_configurations(active)

    assert handler not in get_registered_handlers()
    assert configuration.flushed


def test_activation_prefers_leaf_classes():
    """The override rule: a subclass replaces every class it inherits from,
    an unrelated class is additive - so a repo override of a built-in never
    doubles the reports."""

    class Base(TelemetryConfiguration):
        def get_handler(self, orchestrator_config):
            return _ListHandler()

    class Override(Base):
        pass

    class Additive(TelemetryConfiguration):
        def get_handler(self, orchestrator_config):
            return _ListHandler()

    active = activate_telemetry_configurations([Base, Override, Additive], None)
    try:
        assert {type(configuration) for configuration, _ in active} == {
            Override,
            Additive,
        }
    finally:
        shutdown_telemetry_configurations(active)


def test_shutdown_failure_is_logged_not_raised():
    """A failing flush on the exit path must not mask the result of the
    run."""

    class Explosive(TelemetryConfiguration):
        def get_handler(self, orchestrator_config):
            return _ListHandler()

        def shutdown(self):
            raise RuntimeError("flush boom")

    active = activate_telemetry_configurations([Explosive], None)

    shutdown_telemetry_configurations(active)

    ((_, handler),) = active
    assert handler not in get_registered_handlers()


def test_configuration_discovered_from_the_repo(e2e_repo, monkeypatch, tmp_path):
    """The configuration-first path end to end: a TelemetryConfiguration in
    a telemetry.py file of the repo activates for a headless run - gated on
    its environment values - reports the error of the run, and is flushed
    and unregistered afterwards."""
    sink = tmp_path / "telemetry.jsonl"
    monkeypatch.setenv("WINSLOW_TEST_TELEMETRY_SINK", str(sink))
    orchestrator = headless_orchestrator(e2e_repo, "my-boom-eligibility")

    with pytest.raises(EligibilityError):
        orchestrator.start()

    records = [json.loads(line) for line in sink.read_text().splitlines()]
    assert [r["kind"] for r in records] == ["unscoped_error", "shutdown"]
    assert records[0]["exc"] == "EligibilityError"
    assert records[0]["workflow"] == "my-boom-eligibility"
    assert records[0]["has_session"] is True
    assert records[1]["mode"] == "headless"
    assert not any(
        type(h).__name__ == "_FileSinkHandler" for h in get_registered_handlers()
    )
