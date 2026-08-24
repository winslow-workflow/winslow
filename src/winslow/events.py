"""The event payloads of the session bus (see SessionBus). An event carries
values, never a live object (the payload rule, see docs/ui-plugins.md). The
event class is the topic: a subscriber passes it to SessionBus.subscribe."""

from dataclasses import dataclass
from enum import Enum


class Origin(Enum):
    """Why a store write happened. RUN is a live transition. SEED is a
    restore write (see Workflow.seed_from_state)."""

    RUN = "run"
    SEED = "seed"


@dataclass(frozen=True)
class TaskStatusEvent:
    """One status write on the task store of the session."""

    key: str
    status: "TaskStatus"  # noqa: F821
    origin: Origin = Origin.RUN


@dataclass(frozen=True)
class ExecutionStatusEvent:
    """One status write on the record store of one batch."""

    task_key: str
    status: "TaskStatus"  # noqa: F821
    batch_uuid: str
    origin: Origin = Origin.RUN


@dataclass(frozen=True)
class BatchCreatedEvent:
    """One admitted batch, published before its first task work."""

    info: "BatchInfo"  # noqa: F821


@dataclass(frozen=True)
class BatchCompletedEvent:
    """One completed batch, published after its final status is set."""

    info: "BatchInfo"  # noqa: F821


@dataclass(frozen=True)
class LogLineEvent:
    """One captured log line of one task in one batch."""

    task_key: str
    batch_uuid: str
    line: str


@dataclass(frozen=True)
class SessionEndedEvent:
    session_id: str
