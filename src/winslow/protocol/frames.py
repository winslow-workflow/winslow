"""The wire envelope base with its JSON round trip, then the frames."""

import re
from dataclasses import dataclass, field

from winslow.protocol.codec import CODEC


def wire_name(name):
    """The snake case wire name of one class name (HelloOk -> hello_ok)."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


@dataclass(frozen=True, kw_only=True)
class Frame:
    """One wire envelope. type is the wire name of the class (HelloOkFrame
    -> hello_ok) and the constructor names the fields, so one declaration
    builds and validates a frame. types maps each type to its class, filled
    as the classes are defined."""

    type = None
    types = {}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # A class named *Frame declares a wire type. Any other subclass is an
        # envelope of its base and inherits the type (see Read.envelope).
        declared = vars(cls).get("type")
        if declared is None and cls.__name__.endswith("Frame"):
            declared = cls.type = wire_name(cls.__name__.removesuffix("Frame"))
        if declared is not None:
            Frame.types[declared] = cls


def encode_frame(frame):
    """The JSON text of one frame, with type in front of the fields."""
    return CODEC.encode(frame, type=frame.type)


def decode_frame(raw):
    """The frame object of one JSON dict, by its type (see Frame.types). The
    lane frames register too (see Lane.frame)."""
    frame_class = Frame.types.get(raw.get("type"))
    if frame_class is None:
        raise ValueError(
            f"{raw.get('type')!r} names no frame. The frames are {sorted(Frame.types)}."
        )
    return CODEC.decode(frame_class, raw)


# --- the frames --------------------------------------------------------------------

# Client to server.


@dataclass(frozen=True, kw_only=True)
class HelloFrame(Frame):
    token: str | None = None
    ticket: str | None = None


@dataclass(frozen=True, kw_only=True)
class SubscribeFrame(Frame):
    session_id: str
    request_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class UnsubscribeFrame(SubscribeFrame):
    """The fields of a subscribe, under the unsubscribe type."""


@dataclass(frozen=True, kw_only=True)
class TaskLogSubscribeFrame(Frame):
    session_id: str
    task_key: str
    request_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class TaskLogUnsubscribeFrame(TaskLogSubscribeFrame):
    """The fields of a task log subscribe, under the unsubscribe type."""


@dataclass(frozen=True, kw_only=True)
class RequestFrame(Frame):
    """The base of every read envelope. kind names the port read, and the
    envelope of that read validates the frame (see Read.envelope)."""

    kind: str
    request_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class ActionFrame(Frame):
    """The fields fill the action dataclass (see Action.build)."""

    session_id: str
    action: str
    request_id: str | None = None
    fields: dict = field(default_factory=dict)


# Server to client: the handshake and the control replies.


@dataclass(frozen=True, kw_only=True)
class HelloOkFrame(Frame):
    user: str


@dataclass(frozen=True, kw_only=True)
class HelloErrorFrame(Frame):
    """detail carries the validation report of a malformed hello. The close
    frame repeats reason only, because a close reason holds 123 bytes."""

    reason: str
    detail: str | None = None


@dataclass(frozen=True, kw_only=True)
class ErrorFrame(Frame):
    """detail carries a server traceback for a traceback read (see Read)."""

    reason: str
    request_id: str | None = None
    detail: str | None = None


@dataclass(frozen=True, kw_only=True)
class ResultFrame(Frame):
    request_id: str | None
    kind: str
    result: object


@dataclass(frozen=True, kw_only=True)
class AckFrame(Frame):
    """ack is the Ack or BatchAck (see winslow.actions). The codec writes it
    as a dict, and the client reads that dict."""

    request_id: str | None
    ack: object


@dataclass(frozen=True, kw_only=True)
class TaskLogBacklogFrame(Frame):
    """The reply of a task log subscribe: the buffered lines of the task."""

    request_id: str | None
    session_id: str
    task_key: str
    lines: tuple


# Server to client: the first frame of a session subscription.


@dataclass(frozen=True, kw_only=True)
class SnapshotFrame(Frame):
    """The SessionSnapshot of one session, stamped with the sequence the lane
    frames continue from (see EventBridge.snapshot). The codec writes it as a
    dict and the client decodes it back (see SessionLane)."""

    session_id: str
    seq: int
    snapshot: object
