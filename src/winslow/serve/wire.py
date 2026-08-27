"""The wire vocabulary shared by the serve transports: the action frame
builder and the row shapes of sessions, descriptors, caches, and history.
Both the websocket layer and the MCP tools read these."""

from dataclasses import asdict

from winslow.actions import (
    CheckTasks,
    ClearCacheEntries,
    EndSession,
    LoadCacheEntries,
    RunTasks,
    SetBatchOptions,
    StopBatch,
)
from winslow.cache import (
    MISSING,
    DisplayStyle,
    EntryState,
    StorageRecord,
    declared_entries,
    render_value,
    resolve_snapshot_cap,
)
from winslow.util import safe_repr


class FrameTypes:
    """The top-level "type" values of a frame (see Connection.handle_frame).
    hello and hello_ok belong to the handshake, before a Connection exists."""

    HELLO = "hello"
    HELLO_OK = "hello_ok"
    HELLO_ERROR = "hello_error"
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"
    SUBSCRIBE_TASK_LOG = "subscribe_task_log"
    UNSUBSCRIBE_TASK_LOG = "unsubscribe_task_log"
    ACTION = "action"
    REQUEST = "request"


class Actions:
    """The action names of the wire protocol (see ACTION_CLASSES). A frame
    names one of these under "action"."""

    RUN_TASKS = "run_tasks"
    CHECK_TASKS = "check_tasks"
    STOP_BATCH = "stop_batch"
    END_SESSION = "end_session"
    SET_BATCH_OPTIONS = "set_batch_options"
    LOAD_CACHE_ENTRIES = "load_cache_entries"
    CLEAR_CACHE_ENTRIES = "clear_cache_entries"


class Requests:
    """The request kinds of the wire protocol (see Connection.run_request).
    A frame names one of these under "kind"."""

    CREATE_SESSION = "create_session"
    DESCRIPTORS = "descriptors"
    HISTORY = "history"
    LOG_TAIL = "log_tail"
    TASK_DETAIL = "task_detail"
    ROSTER = "roster"
    CACHES = "caches"
    CACHE_VALUE = "cache_value"
    RECORD_DETAIL = "record_detail"
    BATCH_OPTIONS = "batch_options"
    SESSION_PARAMS = "session_params"
    APPLY_FILTER = "apply_filter"
    MANIFESTS = "manifests"
    RESTORE_SESSION = "restore_session"


# The frame names the action, the fields fill the dataclass (see
# winslow.actions).
ACTION_CLASSES = {
    Actions.RUN_TASKS: RunTasks,
    Actions.CHECK_TASKS: CheckTasks,
    Actions.STOP_BATCH: StopBatch,
    Actions.END_SESSION: EndSession,
    Actions.SET_BATCH_OPTIONS: SetBatchOptions,
    Actions.LOAD_CACHE_ENTRIES: LoadCacheEntries,
    Actions.CLEAR_CACHE_ENTRIES: ClearCacheEntries,
}


def session_row(session):
    workflow = session.workflow
    completed, problematic, total = session.task_status_summary
    return {
        "session_id": session.session_id,
        "workflow": str(workflow),
        "status": session.status.name,
        "display_name": workflow.get_display_name(),
        "instance_name": workflow.instance_name,
        "identifier_suffix": workflow.identifier_suffix,
        "started_at": session.start,
        "elapsed": session.elapsed,
        "task_status_summary": {
            "completed": completed,
            "problematic": problematic,
            "total": total,
        },
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
    if "entries" in fields:
        fields["entries"] = tuple(tuple(pair) for pair in fields["entries"])
    try:
        return action_class(**fields)
    except TypeError as exc:
        raise ValueError(f"bad fields for {name}: {exc}") from None


def option_row(name, option, current=None):
    """One ConfigOption as form metadata. Defaults travel as formatted
    strings: right for a form, accepted for an agent (see serve-spec 6.1).
    `initial` names the value a form should prefill. It is the live parsed
    value when the caller passes one, an orchestrator override already
    parsed from the CLI, and the declared default otherwise."""
    initial = option.default if current is None else current
    return {
        "name": name,
        "help": option.help_text,
        "default": option.format_value(option.default),
        "initial": option.format_value(initial),
        "required": option.required,
        "choices": (
            [str(choice) for choice in option.choices] if option.choices else None
        ),
        "multiselect": option.multiselect,
        "type": option.type.__name__ if option.type else None,
        "identifier": option.identifier,
        "depends_on": list(option.depends_on),
        "action": option.action,
        "const": option.const,
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
        option_row(
            option_name,
            option,
            current=getattr(orchestrator.orchestrator_config, option_name, None),
        )
        for option_name, option in orchestrator.config_meta.items()
        if option.show_on_ui
    ]
    return {"workflows": workflows, "overrides": overrides}


def _task_detail_row(status, record):
    return {
        "status": status.name,
        "started_at": record.started_at.timestamp() if record.started_at else None,
        "duration": record.duration,
        "last_log": record.last_log,
    }


def history_rows(session):
    """One row per batch, with the per-task outcomes of its record store.
    tasks_detail adds started_at, duration and the last log line per task,
    so a client joining mid-flight renders rows without one log_tail per
    task."""
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
                "tasks_detail": (
                    {
                        key: _task_detail_row(status, store.get_record(key))
                        for key, status in store.items()
                    }
                    if store is not None
                    else {}
                ),
            }
        )
    return rows


def record_detail_payload(record):
    """The full capture of one execution record: its TaskInfo, its phase
    timeline, and its transient and cache snapshots (see ExecutionRecord)."""
    return {
        "info": asdict(record.info),
        "phases": [
            {
                "phase": span.phase.value,
                "started_at": span.started_at.timestamp(),
                "completed_at": (
                    span.completed_at.timestamp() if span.completed_at else None
                ),
                "duration": span.duration,
            }
            for span in record.phases
        ],
        "transient_snapshots": {
            phase.value: snapshot
            for phase, snapshot in record.transient_snapshots.items()
        },
        "cache_snapshots": {
            phase.value: [asdict(snapshot) for snapshot in snapshots]
            for phase, snapshots in record.cache_snapshots.items()
        },
    }


def _display_style_label(display_style):
    if display_style is DisplayStyle.TREE:
        return "tree"
    if display_style is DisplayStyle.RAW:
        return "raw"
    return "custom"


def _entry_value_preview(cache, entry_name):
    record = cache.peek(entry_name)
    return safe_repr(record.value) if isinstance(record, StorageRecord) else None


def all_caches(workflow):
    return (*workflow.workflow_cache.caches(), *workflow.global_cache.caches())


def resolve_cache(workflow, name):
    """The live cache of this session named `name`, or None."""
    for cache in all_caches(workflow):
        if cache.get_name() == name:
            return cache
    return None


def cache_card_payload(cache):
    """One cache card: identity, storage, and the declared entries with
    their display style and their current value preview."""
    entries = declared_entries(type(cache))
    infos = cache.inspect()
    return {
        "name": cache.get_name(),
        "scope": cache.scope,
        "docstring": type(cache).__doc__,
        "storage": cache.describe_storage(),
        "entries": [
            {"name": name, "display_style": _display_style_label(entry.display_style)}
            for name, entry in entries.items()
        ],
        "info": [asdict(info) for info in infos],
        "values": {
            info.entry_name: _entry_value_preview(cache, info.entry_name)
            for info in infos
            if info.written_at is not None
        },
    }


def caches_payload(workflow):
    return {"caches": [cache_card_payload(cache) for cache in all_caches(workflow)]}


def cache_value_payload(cache, entry_name):
    """The rendered form of one entry value, built server-side. The live
    modal and a wire client thus render the same text the history path
    already serves (see CacheReadSnapshot)."""
    info = next(i for i in cache.inspect() if i.entry_name == entry_name)
    error = asdict(info.error) if info.error is not None else None
    record = cache.peek(entry_name)
    if record is MISSING:
        return {
            "cache_name": cache.get_name(),
            "entry_name": entry_name,
            "state": EntryState.COLD.value,
            "encoding": None,
            "rendered": None,
            "summary": None,
            "written_at": None,
            "error": error,
        }
    if record is EntryState.COMPUTING:
        return {
            "cache_name": cache.get_name(),
            "entry_name": entry_name,
            "state": EntryState.COMPUTING.value,
            "encoding": None,
            "rendered": None,
            "summary": None,
            "written_at": None,
            "error": error,
        }
    display_style = declared_entries(type(cache))[entry_name].display_style
    rendered, summary, encoding = render_value(
        record.value, resolve_snapshot_cap(type(cache)), display_style
    )
    return {
        "cache_name": cache.get_name(),
        "entry_name": entry_name,
        "state": info.state.value,
        "encoding": encoding.value,
        "rendered": rendered,
        "summary": summary,
        "written_at": record.written_at,
        "error": error,
    }


def session_params_payload(workflow):
    """settings_snapshot plus the resolved workflow_config values (see
    WorkflowParams, the local modal with the same content)."""
    return {
        "settings": workflow.settings_snapshot,
        "workflow_config": {
            name: workflow.config_meta[name].format_value(
                getattr(workflow.workflow_config, name, None)
            )
            for name in workflow.config_option_names
        },
    }


def manifest_row(manifest):
    return {
        "session_id": manifest.session_id,
        "workflow_class": manifest.workflow_class,
        "orchestrator_overrides": manifest.orchestrator_overrides,
        "workflow_values": manifest.workflow_values,
    }
