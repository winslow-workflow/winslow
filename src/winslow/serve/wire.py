"""The wire vocabulary shared by the serve transports: the action frame
builder and the row shapes of sessions, descriptors, and history. Both the
websocket layer and the MCP tools read these."""

from winslow.actions import (
    CheckTasks,
    EndSession,
    RunTasks,
    SetBatchOptions,
    StopBatch,
)

# The frame names the action, the fields fill the dataclass (see
# winslow.actions).
ACTION_CLASSES = {
    "run_tasks": RunTasks,
    "check_tasks": CheckTasks,
    "stop_batch": StopBatch,
    "end_session": EndSession,
    "set_batch_options": SetBatchOptions,
}


def session_row(session):
    return {
        "session_id": session.session_id,
        "workflow": str(session.workflow),
        "status": session.status.name,
    }


def build_action(name, fields):
    """The action dataclass for one wire frame. Raises ValueError with a
    directional message on an unknown name or on bad fields."""
    action_class = ACTION_CLASSES.get(name)
    if action_class is None:
        raise ValueError(
            f"{name!r} names no action. The actions are {sorted(ACTION_CLASSES)}."
        )
    fields = dict(fields or {})
    if "keys" in fields:
        fields["keys"] = tuple(fields["keys"])
    try:
        return action_class(**fields)
    except TypeError as exc:
        raise ValueError(f"bad fields for {name}: {exc}") from None


def option_row(name, option):
    """One ConfigOption as form metadata. Defaults travel as formatted
    strings: right for a form, accepted for an agent (see serve-spec 6.1)."""
    return {
        "name": name,
        "help": option.help_text,
        "default": option.format_value(option.default),
        "required": option.required,
        "choices": (
            [str(choice) for choice in option.choices] if option.choices else None
        ),
        "multiselect": option.multiselect,
        "type": option.type.__name__ if option.type else None,
        "identifier": option.identifier,
        "depends_on": list(option.depends_on),
    }


def descriptor_rows(orchestrator):
    """The parameter context of the serve process: one row per collected
    workflow (the `values` of create_session), plus the orchestrator options
    the start form shows (the `overrides`)."""
    workflows = []
    for name in orchestrator.workflow_registry.names:
        workflow_kls = orchestrator.workflow_registry[name]
        workflows.append(
            {
                "workflow": name,
                "options": [
                    option_row(option_name, option)
                    for option_name, option in workflow_kls.config_meta.items()
                    if option.show_on_ui
                ],
            }
        )
    overrides = [
        option_row(option_name, option)
        for option_name, option in orchestrator.config_meta.items()
        if option.show_on_ui
    ]
    return {"workflows": workflows, "overrides": overrides}


def history_rows(session):
    """One row per batch, with the per-task outcomes of its record store."""
    runner = session.workflow.runner
    rows = []
    for batch in runner.batches:
        store = runner.record_store(batch.uuid)
        rows.append(
            {
                "uuid": batch.uuid,
                "action": batch.action.name,
                "status": batch.status.name,
                "task_count": batch.task_count,
                "created_at": batch.created_at.timestamp(),
                "completed_at": (
                    batch.completed_at.timestamp() if batch.completed_at else None
                ),
                "tasks": (
                    {key: status.name for key, status in store.items()}
                    if store is not None
                    else {}
                ),
            }
        )
    return rows
