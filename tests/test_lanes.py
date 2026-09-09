"""The lane machinery and the lane declarations: the tick rules and the frame
round trip of every lane (see winslow.protocol.lanes)."""

import json

import pytest

from winslow.client.base import SessionClient
from winslow.events import (
    BatchCreatedEvent,
    ExecutionStatusEvent,
    LogLineEvent,
    Origin,
    SessionEndedEvent,
    TaskStatusEvent,
)
from winslow.exceptions import MisconfigurationError
from winslow.model import BatchInfo, CacheUpdatedEvent, SessionLogEvent, TaskLogEvent
from winslow.protocol.frames import Frame, ResultFrame, decode_frame, encode_frame
from winslow.protocol.lanes import (
    BatchCreatedLane,
    BatchedLane,
    CacheUpdatedLane,
    ExecutionStatusLane,
    Lane,
    LogBatchLane,
    SessionEndedLane,
    TaskStatusLane,
    Text,
    decode_lane,
    encode_tick,
)
from winslow.task.status import TaskStatus


def _stamped(lane, fields, seq=1):
    return lane.get_frame()(session_id="s-1", seq=seq, **fields)


def _info(uuid):
    return BatchInfo(
        uuid=uuid,
        action="RUN",
        status="RUNNING",
        task_count=1,
        tasks={"k": "K"},
        options=None,
        created_at=1.0,
        started_at=1.0,
        completed_at=None,
        error=None,
    )


def _round_trip(event):
    """One event through the bridge side, the socket text, and the client side."""
    lane = Lane.by_event[type(event)]
    (fields,) = lane.encode([event])
    raw = json.loads(encode_frame(_stamped(lane, fields)))
    return decode_lane(decode_frame(raw))


# --- declarations -----------------------------------------------------------------


def test_every_lane_registers_by_type_and_by_event():
    assert Lane.by_type["task_status"] is TaskStatusLane
    assert Lane.by_event[TaskStatusEvent] is TaskStatusLane
    assert set(Lane.by_type) == {
        "task_status",
        "execution_status",
        "batch_created",
        "batch_completed",
        "session_ended",
        "cache_updated",
        "log_batch",
        "session_log_batch",
        "task_log_batch",
    }


def test_a_lane_kind_without_an_event_is_not_a_lane():
    assert None not in Lane.by_type
    assert BatchedLane not in Lane.by_type.values()


def test_a_batched_lane_collects_the_one_undeclared_event_field():
    assert LogBatchLane.get_collected() == "line"
    assert set(LogBatchLane.get_fields()) == {"task_key", "batch_uuid"}


def test_a_batched_lane_refuses_an_ambiguous_collection():
    with pytest.raises(MisconfigurationError, match="exactly one field"):

        class Ambiguous(BatchedLane):
            event = LogLineEvent
            task_key = Text()

    # The refused declaration left no trace.
    assert "ambiguous" not in Lane.by_type
    assert Lane.by_event[LogLineEvent] is LogBatchLane


def test_every_frame_registers_under_its_wire_name():
    """The wire names derive from the class names. This table pins them, so a
    class rename shows up as a failure and not as a silent wire change."""
    assert sorted(Frame.types) == [
        "ack",
        "action",
        "batch_completed",
        "batch_created",
        "cache_updated",
        "error",
        "execution_status",
        "hello",
        "hello_error",
        "hello_ok",
        "log_batch",
        "request",
        "result",
        "session_ended",
        "session_log_batch",
        "snapshot",
        "subscribe",
        "task_log_backlog",
        "task_log_batch",
        "task_log_subscribe",
        "task_log_unsubscribe",
        "task_status",
        "unsubscribe",
    ]


def test_the_lane_frame_registers_with_the_frames():
    assert Frame.types["task_status"] is TaskStatusLane.get_frame()
    assert Frame.types["result"] is ResultFrame
    # A derived request envelope inherits its type and stays out.
    assert Frame.types["request"] is not SessionClient.roster.envelope


# --- the tick ---------------------------------------------------------------------


def test_a_run_of_identical_events_crosses_once():
    events = [
        CacheUpdatedEvent("weather"),
        CacheUpdatedEvent("weather"),
        CacheUpdatedEvent("rates"),
        CacheUpdatedEvent("weather"),
    ]
    assert CacheUpdatedLane.encode(events) == [
        {"cache_name": "weather"},
        {"cache_name": "rates"},
        {"cache_name": "weather"},
    ]


def test_a_repeated_transition_keeps_every_step():
    events = [
        TaskStatusEvent("k", TaskStatus.RUNNING),
        TaskStatusEvent("k", TaskStatus.COMPLETED),
        TaskStatusEvent("k", TaskStatus.RUNNING),
    ]
    assert [f["status"] for f in TaskStatusLane.encode(events)] == [
        "RUNNING",
        "COMPLETED",
        "RUNNING",
    ]


def test_batched_lanes_go_first_in_a_tick():
    events = [
        TaskStatusEvent("k", TaskStatus.RUNNING),
        LogLineEvent(task_key="k", batch_uuid="b", line="one"),
        LogLineEvent(task_key="k", batch_uuid="b", line="two"),
        LogLineEvent(task_key="j", batch_uuid="b", line="three"),
        SessionEndedEvent(session_id="s-1"),
    ]
    pairs = encode_tick(events)
    assert [lane for lane, _ in pairs] == [
        LogBatchLane,
        LogBatchLane,
        TaskStatusLane,
        SessionEndedLane,
    ]
    assert pairs[0][1] == {"task_key": "k", "batch_uuid": "b", "lines": ("one", "two")}


def test_control_frames_keep_their_arrival_order():
    """A batch is created before its first execution status: the client must
    see the frames in that order, across lanes."""
    events = [
        ExecutionStatusEvent("a", TaskStatus.COMPLETED, "b-1"),
        BatchCreatedEvent(info=_info("b-2")),
        ExecutionStatusEvent("b", TaskStatus.RUNNING, "b-2"),
    ]
    assert [lane for lane, _ in encode_tick(events)] == [
        ExecutionStatusLane,
        BatchCreatedLane,
        ExecutionStatusLane,
    ]


# --- the round trip -------------------------------------------------------------


def test_a_status_event_round_trips_by_name_and_value():
    event = TaskStatusEvent("k", TaskStatus.RUNNING, origin=Origin.SEED)
    assert _round_trip(event) == (event,)


def test_a_batch_event_round_trips_its_info():
    info = _info("b-1")
    assert _round_trip(BatchCreatedEvent(info=info)) == (BatchCreatedEvent(info=info),)


def test_a_batched_lane_expands_one_event_per_line():
    events = [
        SessionLogEvent(line="a"),
        SessionLogEvent(line="b"),
        TaskLogEvent(task_key="k", line="c"),
    ]
    frames = [
        decode_frame(json.loads(encode_frame(_stamped(lane, fields))))
        for lane, fields in encode_tick(events)
    ]
    decoded = [event for frame in frames for event in decode_lane(frame)]
    assert decoded == events


def test_the_session_id_rides_on_the_stamp_once():
    (fields,) = SessionEndedLane.encode([SessionEndedEvent(session_id="s-1")])
    assert fields == {}
    raw = json.loads(encode_frame(_stamped(SessionEndedLane, fields)))
    assert raw == {"type": "session_ended", "session_id": "s-1", "seq": 1}
    assert decode_lane(decode_frame(raw)) == (SessionEndedEvent(session_id="s-1"),)


def test_an_unknown_frame_type_names_the_known_ones():
    with pytest.raises(ValueError, match="names no frame"):
        decode_frame({"type": "nope"})
