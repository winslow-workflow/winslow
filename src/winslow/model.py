"""Port DTOs and frame envelopes: the value shapes that cross the serve
boundary (the payload rule, see winslow.events). Every field is JSON-safe.
winslow.codec encodes and decodes these classes; winslow.serve.wire builds
instances of them and serializes each with dataclasses.asdict, so a wire
shape has exactly one declaration.

The existing payload dataclasses (BatchInfo, TaskInfo, CacheEntryInfo,
StatusSnapshot, ...) stay where they are declared today; a later change can
move them here."""

from dataclasses import dataclass, field


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


@dataclass(frozen=True)
class PhaseRow:
    """One entry of a record's phase timeline (see PhaseSpan)."""

    phase: str
    started_at: float
    completed_at: float | None
    duration: float | None


@dataclass(frozen=True)
class RecordDetail:
    """The full capture of one execution record: its TaskInfo, its phase
    timeline, and its transient and cache snapshots (see ExecutionRecord).
    info, transient_snapshots and cache_snapshots stay plain dicts: TaskInfo
    and CacheReadSnapshot are not model dataclasses yet."""

    info: dict
    phases: tuple
    transient_snapshots: dict
    cache_snapshots: dict


@dataclass(frozen=True)
class CacheEntryCard:
    """One declared entry of a cache card, before any value is peeked."""

    name: str
    display_style: str


@dataclass(frozen=True)
class CacheCard:
    """One cache: identity, storage, the declared entries with their
    display style, and the current value preview of each written entry
    (see winslow.serve.wire.cache_card_payload)."""

    name: str
    scope: str
    docstring: str | None
    storage: str
    entries: tuple
    info: tuple
    values: dict


@dataclass(frozen=True)
class CachesPayload:
    """Every cache of one session, in name order."""

    caches: tuple


@dataclass(frozen=True)
class CacheValueView:
    """The rendered form of one cache entry value, built server-side (see
    winslow.serve.wire.cache_value_payload). encoding and rendered stay
    None for a cold or a computing entry."""

    cache_name: str
    entry_name: str
    state: str
    encoding: str | None
    rendered: str | None
    summary: str | None
    written_at: float | None
    error: dict | None


@dataclass(frozen=True)
class SessionParams:
    """settings_snapshot plus the resolved workflow_config values of one
    session (see WorkflowParams, the local modal with the same content)."""

    settings: dict
    workflow_config: dict


@dataclass(frozen=True)
class ManifestRow:
    """One restorable session manifest (see SessionManifest)."""

    session_id: str
    workflow_class: str
    orchestrator_overrides: dict | None
    workflow_values: dict | None
