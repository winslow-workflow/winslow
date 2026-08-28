"""The data model of the session port: every value shape that crosses the
port or the serve boundary (the payload rule, see winslow.events). Every
field is JSON-safe. The local adapter hands these instances through
in-process (see winslow.client.local). winslow.serve.wire serializes each
with dataclasses.asdict and winslow.codec decodes the inbound frames, so a
wire shape has exactly one declaration.

The producing side keeps the from_x classmethods, for example from_task and
from_batch. A constructor that needs core machinery imports it inside the
method, so this module imports nothing from winslow at module level."""

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Optional


# --- inbound frame envelopes -------------------------------------------------


@dataclass(frozen=True)
class ActionFrame:
    """One inbound action frame, decoded at the serve edge before dispatch
    (see winslow.serve.wire.build_action)."""

    type: str
    session_id: str
    action: str
    request_id: str | None = None
    fields: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DescriptorsRequest:
    """A descriptors request (see winslow.serve.wire.Requests.DESCRIPTORS)."""

    type: str
    kind: str
    request_id: str | None = None


@dataclass(frozen=True)
class CreateSessionRequest:
    """A create_session request. overrides and values default to {} at the
    handler, so None and an absent field behave the same."""

    type: str
    kind: str
    workflow: str
    request_id: str | None = None
    overrides: dict | None = None
    values: dict | None = None


@dataclass(frozen=True)
class HistoryRequest:
    type: str
    kind: str
    session_id: str
    request_id: str | None = None


@dataclass(frozen=True)
class LogTailRequest:
    type: str
    kind: str
    session_id: str
    batch_uuid: str
    task_key: str
    request_id: str | None = None
    limit: int | None = None


@dataclass(frozen=True)
class TaskDetailRequest:
    type: str
    kind: str
    session_id: str
    task_key: str
    request_id: str | None = None


@dataclass(frozen=True)
class RosterRequest:
    type: str
    kind: str
    session_id: str
    request_id: str | None = None


@dataclass(frozen=True)
class CachesRequest:
    type: str
    kind: str
    session_id: str
    request_id: str | None = None


@dataclass(frozen=True)
class CacheValueRequest:
    type: str
    kind: str
    session_id: str
    cache_name: str
    entry_name: str
    request_id: str | None = None


@dataclass(frozen=True)
class RecordDetailRequest:
    type: str
    kind: str
    session_id: str
    batch_uuid: str
    task_key: str
    request_id: str | None = None


@dataclass(frozen=True)
class BatchOptionsRequest:
    type: str
    kind: str
    session_id: str
    request_id: str | None = None


@dataclass(frozen=True)
class SessionParamsRequest:
    type: str
    kind: str
    session_id: str
    request_id: str | None = None


@dataclass(frozen=True)
class ApplyFilterRequest:
    type: str
    kind: str
    session_id: str
    query: str
    request_id: str | None = None
    builtin_only: bool = False


@dataclass(frozen=True)
class ManifestsRequest:
    type: str
    kind: str
    request_id: str | None = None


@dataclass(frozen=True)
class RestoreSessionRequest:
    type: str
    kind: str
    session_id: str
    request_id: str | None = None


@dataclass(frozen=True)
class SubscribeFrame:
    """One inbound subscribe or unsubscribe frame, decoded at the serve
    edge. unsubscribe reads only session_id."""

    type: str
    session_id: str
    request_id: str | None = None


@dataclass(frozen=True)
class TaskLogSubscribeFrame:
    """One inbound subscribe_task_log or unsubscribe_task_log frame."""

    type: str
    session_id: str
    task_key: str
    request_id: str | None = None


# --- the cache value shapes --------------------------------------------------


class EntryState(StrEnum):
    """The freshness of one entry, derived at peek time (see BaseCache.inspect)."""

    COLD = "cold"
    WARM = "warm"
    STALE = "stale"
    # A loader produces the value right now. A live observation only: the
    # state never appears in a history snapshot.
    COMPUTING = "computing"
    # A delete or a loader failed on the entry. The error context of the
    # projection names the operation and the layer (see CacheEntryError).
    ERRORED = "errored"


# States with a trustworthy value: ERRORED can carry a leftover one.
PREVIEWABLE_STATES = frozenset((EntryState.WARM, EntryState.STALE))


class ErrorOrigin(StrEnum):
    """The operation that left an entry in the ERRORED state."""

    DELETE = "delete"
    LOAD = "load"


@dataclass(frozen=True)
class CacheEntryError:
    """The error context of one entry: the failed operation and its layer.
    Plain strings only, so the context stays wire-ready. A successful write
    of the entry clears it (see BaseCache._entry_value)."""

    origin: ErrorOrigin
    tier: Optional[str]  # the failing storage layer; None for a loader error
    message: str
    at: float
    # The traceback, formatted at failure time: a string retains no frame,
    # so the context stays GC-safe and a value view can show the full cause.
    traceback: Optional[str] = None


class SnapshotEncoding(StrEnum):
    """What the rendered field of a snapshot holds. TEXT displays as it is;
    JSON deserializes back into a value for a tree view."""

    TEXT = "text"
    JSON = "json"


@dataclass(frozen=True)
class CacheReadSnapshot:
    """One recorded cache read of a task phase, rendered and bounded. Plain
    strings only, so a history record outlives the session and its caches.
    A summary marks a bounded rendering; a full rendering carries none."""

    scope: str
    cache_name: str
    entry_name: str
    written_at: float
    rendered: str
    summary: Optional[str]
    encoding: SnapshotEncoding


@dataclass(frozen=True)
class CacheEntryInfo:
    """The projection of one cache entry for a UI layer. Plain scalar fields
    only, so the projection is wire-ready. It never carries the value."""

    scope: str
    cache_name: str
    entry_name: str
    state: EntryState
    written_at: Optional[float]
    ttl: Optional[float]
    eager: bool
    depends_on: tuple
    storage: str
    error: Optional[CacheEntryError]


# --- the task view model -----------------------------------------------------

# The value of a property that no automatic capture evaluated (see
# TaskInfo.from_task). Only the on-demand capture runs a getter.
NOT_EVALUATED = "<not evaluated>"


@dataclass(frozen=True)
class SourceNode:
    """A node in the inheritance source tree of a task."""

    name: str
    module: str
    source: str
    path: str  # absolute source file, or None
    children: tuple  # tuple[SourceNode, ...]


@dataclass(frozen=True)
class TaskRef:
    """A renderable pointer to another task: what a dependency row needs. No
    nested dependencies, so a TaskInfo stays bounded on a deep graph."""

    key: str
    label: str
    is_premier: bool
    is_terminal: bool
    is_noop: bool

    def __str__(self):
        return self.label

    @classmethod
    def from_task(cls, task):
        return cls(
            key=task.identity_key,
            label=str(task),
            is_premier=task.is_premier,
            is_terminal=task.is_terminal,
            is_noop=task.is_noop,
        )


@dataclass(frozen=True, eq=False)
class TaskInfo:
    """The value view model of a task: plain values only, so asdict is
    JSON-serializable and history can hold it without a retention of the task.

    from_task has two depths. The stub, the default, carries the identity and
    the dependency refs. The full capture adds attributes, docs, source and
    transients, and it evaluates a getter only with evaluate=True, which only
    the on-demand detail view passes. Equality and hash use the key."""

    key: str
    label: str
    name: str
    is_premier: bool
    is_terminal: bool
    is_noop: bool
    task_class: str
    index: int
    groups: tuple = ()
    parameters: dict = None
    dependencies: tuple = ()
    premier_dependencies: tuple = ()
    terminal_dependencies: tuple = ()
    # The trust fields of the check_ttl rule, from the session snapshots. None
    # means no verification on record, or no TTL (see Workflow.task_info).
    checked_at: float = None
    effective_ttl: float = None
    # Full-capture fields. None marks a stub, an empty tuple marks a capture
    # that found nothing.
    attributes: tuple = None
    docs: tuple = None
    source: SourceNode = None
    transients: tuple = None

    def __str__(self):
        return self.label

    def __hash__(self):
        return hash(self.key)

    def __eq__(self, other):
        if not isinstance(other, TaskInfo):
            return NotImplemented
        return self.key == other.key

    def get_name(self):
        return self.name

    def get_groups(self):
        return frozenset(self.groups)

    @property
    def groups_readable(self):
        return ", ".join(self.groups) if self.groups else None

    @classmethod
    def from_task(
        cls,
        task,
        full=False,
        evaluate=False,
        root_dir=None,
        checked_at=None,
        effective_ttl=None,
    ):
        # The capture machinery walks live classes and files, so it stays in
        # winslow.task.info; only the value shape lives here.
        from winslow.decorators import declared_transient_properties
        from winslow.task.info import (
            _attribute_sections,
            _display_parameters,
            _safe_sourcefile,
            _source_tree,
            _task_docs,
        )

        full = full or evaluate
        deps = task.dependent_tasks
        task_cls = task.__class__

        return cls(
            key=task.identity_key,
            checked_at=checked_at,
            effective_ttl=effective_ttl,
            label=str(task),
            name=task.instance_name,
            is_terminal=task.is_terminal,
            is_premier=task.is_premier,
            is_noop=task.is_noop,
            index=task._index,
            # The real source file through inspect, and not the synthetic scoped
            # __module__.
            task_class=f"{task_cls.__qualname__} ({_safe_sourcefile(task_cls) or task_cls.__module__})",
            groups=tuple(sorted(task.get_groups())),
            parameters=_display_parameters(task) or None,
            dependencies=tuple(
                TaskRef.from_task(d)
                for d in deps
                if not (d.is_premier or d.is_terminal)
            ),
            premier_dependencies=tuple(
                TaskRef.from_task(d) for d in deps if d.is_premier
            ),
            terminal_dependencies=tuple(
                TaskRef.from_task(d) for d in deps if d.is_terminal
            ),
            attributes=_attribute_sections(task, root_dir, evaluate) if full else None,
            docs=_task_docs(task) if full else None,
            source=_source_tree(task_cls) if full else None,
            transients=(
                tuple(sorted(declared_transient_properties(task_cls))) if full else None
            ),
        )


# --- the batch and session state shapes --------------------------------------


@dataclass(frozen=True)
class BatchInfo:
    """The value snapshot of one batch, for events and the wire (the payload
    rule, see winslow.events). Enum values travel by name, timestamps as epoch
    seconds."""

    uuid: str
    action: str
    status: str
    task_count: int
    # The roster, {identity key: label} (see BatchRecord.tasks).
    tasks: dict
    # The batch option snapshot of the execution context, without the uuid.
    options: dict | None
    created_at: float
    started_at: float | None
    completed_at: float | None
    # The message of the framework error that aborted the batch, or None.
    error: str | None = None

    @classmethod
    def from_batch(cls, batch, tasks):
        context = batch.execution_context
        options = asdict(context) if context is not None else None
        if options is not None:
            options.pop("batch_uuid")
        return cls(
            uuid=batch.uuid,
            action=batch.action.name,
            status=batch.status.name,
            task_count=batch.task_count,
            tasks={task.identity_key: str(task) for task in tasks},
            options=options,
            created_at=batch.created_at.timestamp(),
            started_at=(
                batch.started_at.timestamp() if batch.started_at else None
            ),
            completed_at=(
                batch.completed_at.timestamp() if batch.completed_at else None
            ),
            error=batch.error,
        )


@dataclass(frozen=True)
class BatchRow:
    """One batch of a session snapshot: the identity and the lifecycle
    stamps. HistoryRow is the row that adds the per-task outcomes."""

    uuid: str
    action: str
    status: str
    task_count: int
    created_at: float
    completed_at: float | None

    @classmethod
    def from_batch(cls, batch):
        return cls(
            uuid=batch.uuid,
            action=batch.action.name,
            status=batch.status.name,
            task_count=batch.task_count,
            created_at=batch.created_at.timestamp(),
            completed_at=(
                batch.completed_at.timestamp() if batch.completed_at else None
            ),
        )


@dataclass(frozen=True)
class TaskOutcome:
    """The outcome of one task in one batch: the status of the record store
    plus the record fields a history row shows."""

    status: str
    started_at: float | None
    duration: float | None
    last_log: str

    @classmethod
    def from_record(cls, status, record):
        return cls(
            status=status.name,
            started_at=(
                record.started_at.timestamp() if record.started_at else None
            ),
            duration=record.duration,
            last_log=record.last_log,
        )


@dataclass(frozen=True)
class HistoryRow:
    """One batch of the history, with the per-task outcomes of its record
    store. A client that subscribes after the batch renders these rows
    without one log_tail call per task."""

    uuid: str
    action: str
    status: str
    task_count: int
    created_at: float
    completed_at: float | None
    tasks: dict  # {identity key: TaskOutcome}

    @classmethod
    def from_batch(cls, batch, store):
        return cls(
            uuid=batch.uuid,
            action=batch.action.name,
            status=batch.status.name,
            task_count=batch.task_count,
            created_at=batch.created_at.timestamp(),
            completed_at=(
                batch.completed_at.timestamp() if batch.completed_at else None
            ),
            tasks=(
                {
                    key: TaskOutcome.from_record(status, store.get_record(key))
                    for key, status in store.items()
                }
                if store is not None
                else {}
            ),
        )


@dataclass(frozen=True)
class StatusSnapshot:
    """The latest snapshot of one task in one session. The key is the task
    identity key; the status is a TaskStatus name; checked_at is a wall-clock
    epoch."""

    key: str
    status: str
    checked_at: float


# --- the session rows and snapshots -------------------------------------------


@dataclass(frozen=True)
class TaskStatusSummary:
    """The (completed, problematic, total) counts of one session (see
    Session.task_status_summary)."""

    completed: int
    problematic: int
    total: int


@dataclass(frozen=True)
class SessionRow:
    """One row of the session list, and the shape of a session's entry in
    the hello snapshot (see winslow.serve.wire.session_row)."""

    session_id: str
    workflow: str
    status: str
    display_name: str
    instance_name: str
    identifier_suffix: str
    started_at: float
    elapsed: float
    task_status_summary: TaskStatusSummary

    @classmethod
    def from_session(cls, session):
        workflow = session.workflow
        completed, problematic, total = session.task_status_summary
        return cls(
            session_id=session.session_id,
            workflow=str(workflow),
            status=session.status.name,
            display_name=workflow.get_display_name(),
            instance_name=workflow.instance_name,
            identifier_suffix=workflow.identifier_suffix,
            started_at=session.start,
            elapsed=session.elapsed,
            task_status_summary=TaskStatusSummary(
                completed=completed, problematic=problematic, total=total
            ),
        )


@dataclass(frozen=True)
class SessionSnapshot:
    """The current state of one session: the task statuses, the batch rows,
    the session log backlog, and the session meta. The subscribe reply and
    the local snapshot read serve the same shape (see EventBridge.snapshot)."""

    session_id: str
    workflow: str
    status: str
    tasks: dict  # {identity key: TaskStatus name}
    session_log_backlog: tuple
    batches: tuple  # tuple[BatchRow, ...]

    @classmethod
    def from_session(cls, session):
        workflow = session.workflow
        return cls(
            session_id=session.session_id,
            workflow=str(workflow),
            status=session.status.name,
            tasks={key: status.name for key, status in workflow.store.items()},
            session_log_backlog=(
                tuple(session.log_buffer.lines)
                if session.log_buffer is not None
                else ()
            ),
            batches=tuple(
                BatchRow.from_batch(batch) for batch in workflow.runner.batches
            ),
        )


# --- the record detail --------------------------------------------------------


@dataclass(frozen=True)
class PhaseRow:
    """One entry of a record's phase timeline (see PhaseSpan)."""

    phase: str
    started_at: float
    completed_at: float | None
    duration: float | None

    @classmethod
    def from_span(cls, span):
        return cls(
            phase=span.phase.value,
            started_at=span.started_at.timestamp(),
            completed_at=(
                span.completed_at.timestamp() if span.completed_at else None
            ),
            duration=span.duration,
        )


@dataclass(frozen=True)
class RecordDetail:
    """The full capture of one execution record: its TaskInfo, its phase
    timeline, and its transient and cache snapshots (see ExecutionRecord).
    The snapshot dicts key by the phase name."""

    info: TaskInfo
    phases: tuple  # tuple[PhaseRow, ...]
    transient_snapshots: dict
    cache_snapshots: dict  # {phase name: tuple[CacheReadSnapshot, ...]}

    @classmethod
    def from_record(cls, record):
        return cls(
            info=record.info,
            phases=tuple(PhaseRow.from_span(span) for span in record.phases),
            transient_snapshots={
                phase.value: snapshot
                for phase, snapshot in record.transient_snapshots.items()
            },
            cache_snapshots={
                phase.value: tuple(snapshots)
                for phase, snapshots in record.cache_snapshots.items()
            },
        )


# --- the cache cards and value views -------------------------------------------


def _display_style_label(display_style):
    from winslow.cache import DisplayStyle

    if display_style is DisplayStyle.TREE:
        return "tree"
    if display_style is DisplayStyle.RAW:
        return "raw"
    return "custom"


def _entry_value_preview(cache, entry_name):
    from winslow.cache import StorageRecord
    from winslow.util import safe_repr

    record = cache.peek(entry_name)
    return safe_repr(record.value) if isinstance(record, StorageRecord) else None


@dataclass(frozen=True)
class CacheEntryCard:
    """One declared entry of a cache card, before any value is peeked."""

    name: str
    display_style: str


@dataclass(frozen=True)
class CacheCard:
    """One cache: identity, storage, the declared entries with their
    display style, and the current value preview of each written entry."""

    name: str
    scope: str
    docstring: str | None
    storage: str
    entries: tuple  # tuple[CacheEntryCard, ...]
    info: tuple  # tuple[CacheEntryInfo, ...]
    values: dict

    @classmethod
    def from_cache(cls, cache):
        from winslow.cache import declared_entries

        entries = declared_entries(type(cache))
        infos = tuple(cache.inspect())
        return cls(
            name=cache.get_name(),
            scope=cache.scope,
            docstring=type(cache).__doc__,
            storage=cache.describe_storage(),
            entries=tuple(
                CacheEntryCard(
                    name=name,
                    display_style=_display_style_label(entry.display_style),
                )
                for name, entry in entries.items()
            ),
            info=infos,
            values={
                info.entry_name: _entry_value_preview(cache, info.entry_name)
                for info in infos
                if info.state in PREVIEWABLE_STATES
            },
        )


@dataclass(frozen=True)
class CachesPayload:
    """Every cache of one session, in name order."""

    caches: tuple


@dataclass(frozen=True)
class CacheValueView:
    """The rendered form of one cache entry value, built server-side. The
    live modal and a wire client thus render the same text the history path
    already serves (see CacheReadSnapshot). encoding and rendered stay None
    for a cold or a computing entry."""

    cache_name: str
    entry_name: str
    state: str
    encoding: str | None
    rendered: str | None
    summary: str | None
    written_at: float | None
    error: CacheEntryError | None

    @classmethod
    def from_entry(cls, cache, entry_name):
        from winslow.cache import (
            MISSING,
            declared_entries,
            render_value,
            resolve_snapshot_cap,
        )

        info = next(i for i in cache.inspect() if i.entry_name == entry_name)
        record = cache.peek(entry_name)
        if record is MISSING:
            return cls._unrendered(cache, entry_name, EntryState.COLD, info.error)
        if record is EntryState.COMPUTING:
            return cls._unrendered(
                cache, entry_name, EntryState.COMPUTING, info.error
            )
        display_style = declared_entries(type(cache))[entry_name].display_style
        rendered, summary, encoding = render_value(
            record.value, resolve_snapshot_cap(type(cache)), display_style
        )
        return cls(
            cache_name=cache.get_name(),
            entry_name=entry_name,
            state=info.state.value,
            encoding=encoding.value,
            rendered=rendered,
            summary=summary,
            written_at=record.written_at,
            error=info.error,
        )

    @classmethod
    def _unrendered(cls, cache, entry_name, state, error):
        """The view of an entry with no value to render: cold, or a loader
        runs right now."""
        return cls(
            cache_name=cache.get_name(),
            entry_name=entry_name,
            state=state.value,
            encoding=None,
            rendered=None,
            summary=None,
            written_at=None,
            error=error,
        )


# --- the session parameters and manifests --------------------------------------


@dataclass(frozen=True)
class SessionParams:
    """settings_snapshot plus the resolved workflow_config values of one
    session (see WorkflowParams, the local modal with the same content)."""

    settings: dict
    workflow_config: dict

    @classmethod
    def from_workflow(cls, workflow):
        return cls(
            settings=workflow.settings_snapshot,
            workflow_config={
                name: workflow.config_meta[name].format_value(
                    getattr(workflow.workflow_config, name, None)
                )
                for name in workflow.config_option_names
            },
        )


@dataclass(frozen=True)
class ManifestRow:
    """One restorable session manifest (see SessionManifest)."""

    session_id: str
    workflow_class: str
    orchestrator_overrides: dict | None
    workflow_values: dict | None

    @classmethod
    def from_manifest(cls, manifest):
        return cls(
            session_id=manifest.session_id,
            workflow_class=manifest.workflow_class,
            orchestrator_overrides=manifest.orchestrator_overrides,
            workflow_values=manifest.workflow_values,
        )


# --- the form descriptors -------------------------------------------------------


@dataclass(frozen=True)
class OptionRow:
    """One ConfigOption as form metadata. Defaults travel as formatted
    strings: right for a form, accepted for an agent (see serve-spec 6.1).
    initial names the value a form should prefill. It is the live parsed
    value when the caller passes one, an orchestrator override already
    parsed from the CLI, and the declared default otherwise."""

    name: str
    help: str | None
    default: str | None
    initial: str | None
    required: bool
    choices: tuple | None
    multiselect: bool
    type: str | None
    identifier: bool
    depends_on: tuple
    action: str | None
    const: object

    @classmethod
    def from_option(cls, name, option, current=None):
        initial = option.default if current is None else current
        return cls(
            name=name,
            help=option.help_text,
            default=option.format_value(option.default),
            initial=option.format_value(initial),
            required=option.required,
            choices=(
                tuple(str(choice) for choice in option.choices)
                if option.choices
                else None
            ),
            multiselect=option.multiselect,
            type=option.type.__name__ if option.type else None,
            identifier=option.identifier,
            depends_on=tuple(option.depends_on),
            action=option.action,
            const=option.const,
        )


@dataclass(frozen=True)
class WorkflowDescriptor:
    """One collected workflow and the options its start form shows."""

    workflow: str
    options: tuple  # tuple[OptionRow, ...]


@dataclass(frozen=True)
class Descriptors:
    """The parameter context of one process: a descriptor per collected
    workflow (the `values` of create_session), plus the orchestrator options
    the start form shows (the `overrides`). A workflow's initial values come
    from the same CLI-supplied args that prefill the local start form (see
    Orchestrator.collect_workflow_args). A workflow the orchestrator config
    excludes from the local selector is excluded here too."""

    workflows: tuple  # tuple[WorkflowDescriptor, ...]
    overrides: tuple  # tuple[OptionRow, ...]

    @classmethod
    def from_orchestrator(cls, orchestrator):
        workflow_args = orchestrator.collect_workflow_args()
        workflows = []
        for name in orchestrator.workflow_registry.names:
            workflow_kls = orchestrator.workflow_registry[name]
            if not workflow_kls.should_be_initialized(
                orchestrator.orchestrator_config
            ):
                continue
            parsed = workflow_args.get(workflow_kls)
            workflows.append(
                WorkflowDescriptor(
                    workflow=name,
                    options=tuple(
                        OptionRow.from_option(
                            option_name,
                            option,
                            current=getattr(parsed, option_name, None),
                        )
                        for option_name, option in workflow_kls.config_meta.items()
                        if option.show_on_ui
                    ),
                )
            )
        overrides = tuple(
            OptionRow.from_option(
                option_name,
                option,
                current=getattr(
                    orchestrator.orchestrator_config, option_name, None
                ),
            )
            for option_name, option in orchestrator.config_meta.items()
            if option.show_on_ui
        )
        return cls(workflows=tuple(workflows), overrides=overrides)


# --- the port event lanes with no bus event class -------------------------------


@dataclass(frozen=True)
class CacheUpdatedEvent:
    """The repaint trigger of one cache. The port synthesizes it from the
    CacheListener callbacks (see winslow.client.local) or from cache_updated
    frames."""

    cache_name: str


@dataclass(frozen=True)
class SessionLogEvent:
    """One line of the session log lane: the workflow logger stream (init,
    eligibility, cache lines). LogLineEvent stays task-scoped."""

    line: str


@dataclass(frozen=True)
class TaskLogEvent:
    """One live line of a task log subscription (see
    SessionClient.subscribe_task_log)."""

    task_key: str
    line: str
