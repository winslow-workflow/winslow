"""Session creation for the serve process: the same flow the TUI runs at the
start form, on a worker thread (see Winslow._create_workflow; a shared core
function is a recorded follow-up). Returns the live session, registered."""

import logging

from winslow.logger import run_logger_name
from winslow.session import Session
from winslow.task.context import LogContext, scoped_log_context
from winslow.util import generate_id


def create_session(
    orchestrator,
    state_store,
    registry,
    workflow_name,
    orchestrator_overrides=None,
    workflow_values=None,
):
    """Build, initialize, persist, and register one session. Raises with a
    directional message on an unknown workflow; a failure after registration
    marks the session errored and unregisters it."""
    try:
        workflow_kls = orchestrator.workflow_registry[workflow_name]
    except KeyError:
        raise KeyError(
            f"workflow {workflow_name!r} names no collected workflow. "
            f"The workflows are {orchestrator.workflow_registry.names}."
        ) from None

    orchestrator_overrides = orchestrator_overrides or {}
    workflow_values = workflow_values or {}
    session_id = generate_id(workflow_name)
    workflow_logger = logging.getLogger(run_logger_name(session_id))
    workflow_logger.propagate = True

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
        except Exception as exc:
            registry.remove(session_id)
            session.mark_error(exc)
            raise
    return session
