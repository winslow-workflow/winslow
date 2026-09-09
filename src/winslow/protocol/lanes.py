"""The lane machinery, then the lanes: a Field per wire field, the Lane base,
the tick rules, and one Lane class per event."""

import json
from dataclasses import asdict, fields as dataclass_fields, make_dataclass
from itertools import groupby

from winslow.events import (
    BatchCompletedEvent,
    BatchCreatedEvent,
    ExecutionStatusEvent,
    LogLineEvent,
    Origin,
    SessionEndedEvent,
    TaskStatusEvent,
)
from winslow.exceptions import MisconfigurationError
from winslow.model import BatchInfo, CacheUpdatedEvent, SessionLogEvent, TaskLogEvent
from winslow.protocol.codec import CODEC
from winslow.protocol.frames import Frame, wire_name
from winslow.task.status import TaskStatus

# The stamp the bridge puts on every lane frame (see EventBridge._fan_out).
STAMP = ("session_id", "seq")


class Field:
    """One wire field of a lane. encode puts the event value on the frame and
    decode reads it back. wire is the type of the value on the frame."""

    wire = object

    def encode(self, value):
        return value

    def decode(self, value):
        return value


class Text(Field):
    """A field that crosses the wire as it is."""

    wire = str


class ByName(Field):
    """An enum field that crosses the wire as its member name."""

    wire = str

    def __init__(self, enum):
        self.enum = enum

    def encode(self, value):
        return value.name

    def decode(self, value):
        return self.enum[value]


class ByValue(Field):
    """An enum field that crosses the wire as its member value."""

    wire = str

    def __init__(self, enum):
        self.enum = enum

    def encode(self, value):
        return value.value

    def decode(self, value):
        return self.enum(value)


class Dto(Field):
    """A model dataclass field. It crosses as a dict and the codec rebuilds it."""

    wire = dict

    def __init__(self, dto_class):
        self.dto_class = dto_class

    def encode(self, value):
        return asdict(value)

    def decode(self, value):
        return CODEC.decode(self.dto_class, value)


class Lane:
    """One event lane of a session subscription. type is the frame type, the
    wire name of the class (TaskStatusLane -> task_status). event is the event
    class the lane carries. Every Field attribute is one wire field (see
    get_fields). The lanes and their frames register at class creation (see
    by_type, by_event, get_frame)."""

    type = None
    event = None

    # The lanes by frame type and by event class, filled as the classes are
    # defined (see decode_lane, encode_tick).
    by_type = {}
    by_event = {}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # A base with no event is a lane kind (see BatchedLane) and stays unregistered.
        if cls.event is None:
            return
        if "type" not in vars(cls):
            cls.type = wire_name(cls.__name__.removesuffix("Lane"))
        cls._check()
        cls._make_frame()
        Lane.by_type[cls.type] = cls
        Lane.by_event[cls.event] = cls

    @classmethod
    def _check(cls):
        """Refuse a bad declaration before it registers (see BatchedLane._check)."""

    @classmethod
    def get_fields(cls):
        """The Field declarations of the lane by name."""
        return {
            name: value for name, value in vars(cls).items() if isinstance(value, Field)
        }

    @classmethod
    def get_frame(cls):
        """The frame class of the lane, from the frame registry (see _make_frame)."""
        return Frame.types[cls.type]

    @classmethod
    def _make_frame(cls):
        """Build the frame of this lane: the type, the stamp, and the declared
        fields in their wire form. The class registers itself under the lane
        type by its name (see Frame). A declared field the stamp carries
        (session_id) appears once."""
        head = [("session_id", str), ("seq", int)]
        declared = [
            (name, codec.wire)
            for name, codec in cls.get_fields().items()
            if name not in STAMP
        ]
        title = "".join(part.title() for part in cls.type.split("_"))
        make_dataclass(
            f"{title}Frame",
            head + declared + cls._extra_frame_fields(),
            bases=(Frame,),
            frozen=True,
            kw_only=True,
        )

    @classmethod
    def _extra_frame_fields(cls):
        return []

    @classmethod
    def _fields(cls, event):
        """The wire fields of one event as JSON text. The text is the collapse
        key of a tick, and json.loads gives the fields back."""
        fields = {
            name: codec.encode(getattr(event, name))
            for name, codec in cls.get_fields().items()
            if name not in STAMP
        }
        return json.dumps(fields, sort_keys=True)

    @classmethod
    def encode(cls, events):
        """The frame fields of the events of one tick, in order. A run of
        identical events collapses to one frame. A repeated transition
        (A, B, A) keeps every step."""
        return [json.loads(key) for key, _ in groupby(map(cls._fields, events))]

    @classmethod
    def decode(cls, frame):
        """The events of one frame."""
        return (cls.event(**cls._values(frame)),)

    @classmethod
    def _values(cls, frame):
        return {
            name: codec.decode(getattr(frame, name))
            for name, codec in cls.get_fields().items()
        }


class BatchedLane(Lane):
    """One frame per distinct key per tick. The one event field the lane does
    not declare is collected under lines, one item per event, so a burst of
    log lines crosses as one frame."""

    @classmethod
    def _check(cls):
        undeclared = cls._undeclared()
        if len(undeclared) != 1:
            raise MisconfigurationError(
                f"{cls.__name__} must leave exactly one field of {cls.event.__name__} "
                f"undeclared, the one it collects - undeclared: {undeclared}."
            )

    @classmethod
    def _undeclared(cls):
        fields = cls.get_fields()
        return [f.name for f in dataclass_fields(cls.event) if f.name not in fields]

    @classmethod
    def get_collected(cls):
        """The name of the one event field the frame collects under lines."""
        return cls._undeclared()[0]

    @classmethod
    def _extra_frame_fields(cls):
        return [("lines", tuple)]

    @classmethod
    def encode(cls, events):
        groups = {}
        collected = cls.get_collected()
        for event in events:
            groups.setdefault(cls._fields(event), []).append(getattr(event, collected))
        return [
            {**json.loads(key), "lines": tuple(lines)} for key, lines in groups.items()
        ]

    @classmethod
    def decode(cls, frame):
        head = cls._values(frame)
        collected = cls.get_collected()
        return tuple(cls.event(**head, **{collected: line}) for line in frame.lines)


def encode_tick(events):
    """The (lane, fields) pairs of one drain tick. The batched lanes go first,
    so the lines that precede a control frame arrive before it. The control
    frames keep their arrival order: a batch is created before its first status."""
    lanes = [(Lane.by_event[type(event)], event) for event in events]
    by_lane = {}
    for lane, event in lanes:
        if issubclass(lane, BatchedLane):
            by_lane.setdefault(lane, []).append(event)
    batched = [
        (lane, fields)
        for lane, group in by_lane.items()
        for fields in lane.encode(group)
    ]
    keyed = (
        (lane, lane._fields(event))
        for lane, event in lanes
        if not issubclass(lane, BatchedLane)
    )
    control = [(lane, json.loads(key)) for (lane, key), _ in groupby(keyed)]
    return batched + control


def decode_lane(frame):
    """The events of one lane frame. A frame of no lane decodes to an empty tuple."""
    lane = Lane.by_type.get(frame.type)
    return () if lane is None else lane.decode(frame)


# --- the lanes ---------------------------------------------------------------------


class TaskStatusLane(Lane):
    event = TaskStatusEvent
    key = Text()
    status = ByName(TaskStatus)
    origin = ByValue(Origin)


class ExecutionStatusLane(Lane):
    event = ExecutionStatusEvent
    task_key = Text()
    status = ByName(TaskStatus)
    batch_uuid = Text()
    origin = ByValue(Origin)


class BatchCreatedLane(Lane):
    event = BatchCreatedEvent
    info = Dto(BatchInfo)


class BatchCompletedLane(Lane):
    event = BatchCompletedEvent
    info = Dto(BatchInfo)


class SessionEndedLane(Lane):
    event = SessionEndedEvent
    session_id = Text()


class CacheUpdatedLane(Lane):
    event = CacheUpdatedEvent
    cache_name = Text()


class LogBatchLane(BatchedLane):
    event = LogLineEvent
    task_key = Text()
    batch_uuid = Text()


class SessionLogBatchLane(BatchedLane):
    event = SessionLogEvent


class TaskLogBatchLane(BatchedLane):
    event = TaskLogEvent
    task_key = Text()
