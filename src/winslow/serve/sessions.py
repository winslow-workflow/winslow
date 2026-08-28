"""Session creation for the serve process: the same flow the TUI runs at the
start form, on a worker thread (see Winslow._create_workflow; a shared core
function is a recorded follow-up). Returns the live session, registered."""

import logging

from winslow.logger import run_logger_name
from winslow.serve.bridge import SessionLogBuffer
from winslow.session import Session
from winslow.task.context import LogContext, scoped_log_context
from winslow.util import generate_id


def _refuse_value(name, value, option):
    if option.choices and str(value) not in [str(c) for c in option.choices]:
        raise ValueError(
            f"{value!r} is not a choice of {name} - the choices are "
            f"{[str(c) for c in option.choices]}."
        )


def validate_values(workflow_name, workflow_kls, orchestrator, values, overrides):
    """Refuse a bad create payload with direction, before any initialization
    work runs. The descriptors name every option this checks against."""
    known_values = workflow_kls.config_meta
    known_overrides = orchestrator.config_meta
    for name in values:
        if name not in known_values:
            raise ValueError(
                f"{name!r} names no option of {workflow_name} - the options "
                f"are {sorted(known_values)}."
            )
        _refuse_value(name, values[name], known_values[name])
    for name in overrides:
        if name not in known_overrides:
            raise ValueError(
                f"{name!r} names no orchestrator override - the overrides "
                f"are {sorted(known_overrides)}."
            )
        _refuse_value(name, overrides[name], known_overrides[name])
    missing = [
        name
        for name, option in known_values.items()
        if option.required and option.default is None and values.get(name) is None
    ]
    if missing:
        raise ValueError(
            f"{workflow_name} requires {', '.join(missing)} - descriptors "
            f"names the options of the workflow."
        )


def create_session(
    orchestrator,
    state_store,
    registry,
    workflow_name,
    orchestrator_overrides=None,
    workflow_values=None,
    session_id=None,
    seed=False,
):
    """Build, initialize, persist, and register one session. Raises with a
    directional message on an unknown workflow; a failure after registration
    marks the session errored and unregisters it.

    session_id and seed serve a restore: the caller passes the id of the
    stored manifest, so the session rebuilds under it, and seed=True replays
    the stored snapshots onto the store after the eligibility pass (see
    Workflow.seed_from_state)."""
    try:
        workflow_kls = orchestrator.workflow_registry[workflow_name]
    except KeyError:
        raise KeyError(
            f"workflow {workflow_name!r} names no collected workflow. "
            f"The workflows are {orchestrator.workflow_registry.names}."
        ) from None

    orchestrator_overrides = orchestrator_overrides or {}
    workflow_values = workflow_values or {}
    validate_values(
        workflow_name,
        workflow_kls,
        orchestrator,
        workflow_values,
        orchestrator_overrides,
    )
    session_id = session_id or generate_id(workflow_name)
    workflow_logger = logging.getLogger(run_logger_name(session_id))
    workflow_logger.propagate = True
    # Attached before any initialization work runs, so init and eligibility
    # lines survive until a client subscribes (see SessionLogBuffer).
    log_buffer = SessionLogBuffer()
    workflow_logger.addHandler(log_buffer)

    init_log_ctx = LogContext(
        session_id=session_id,
        workflow_name=workflow_name,
        workflow_instance=workflow_name,
        task_name=None,
        task_instance=None,
        batch_uuid=None,
    )
    with scoped_log_context(init_log_ctx):
        workflow = orchestrator.initialize_workflow(
            workflow_kls=workflow_kls,
            orchestrator_overrides=orchestrator_overrides,
            workflow_values=workflow_values,
            logger=workflow_logger,
        )
        session = Session(workflow, session_id=session_id)
        session.log_buffer = log_buffer
        registry.register(session)
        try:
            workflow.initialize_tasks(logger=workflow.logger)
            workflow.check_pipeline_eligibility(logger=workflow.logger)
            # Persistence starts only once the pipeline is runnable: a kill
            # during the initialization above leaves no restore candidate.
            workflow.init_state(
                state_store,
                origin="serve",
                orchestrator_overrides=orchestrator_overrides,
                workflow_values=workflow_values,
            )
            if seed:
                # After the eligibility pass: that pass overwrites earlier
                # status writes (see Workflow.seed_from_state).
                workflow.seed_from_state()
        except Exception as exc:
            registry.remove(session_id)
            session.mark_error(exc)
            raise
    return session
