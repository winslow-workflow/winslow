"""winslow.protocol.codec: one TypeAdapter per DTO class, cached, encoding and
decoding the model dataclasses of winslow.model."""

import pytest

from winslow.protocol.codec import CODEC, ValidationError, _adapter, report
from winslow.client.base import SessionClient
from winslow.protocol.frames import RequestFrame
from winslow.protocol.frames import ActionFrame


def test_decode_builds_the_dataclass_from_a_dict():
    envelope = CODEC.decode(
        ActionFrame,
        {
            "type": ActionFrame.type,
            "session_id": "s-1",
            "action": "run_tasks",
            "request_id": "r-1",
            "fields": {"keys": ["a", "b"]},
        },
    )
    assert envelope == ActionFrame(
        session_id="s-1",
        action="run_tasks",
        request_id="r-1",
        fields={"keys": ["a", "b"]},
    )


def test_decode_fills_declared_defaults():
    envelope = CODEC.decode(
        SessionClient.apply_filter.envelope,
        {
            "type": RequestFrame.type,
            "kind": "apply_filter",
            "session_id": "s-1",
            "query": "a",
        },
    )
    assert envelope.request_id is None
    assert envelope.scope == "tasks"


def test_decode_raises_with_a_directional_message_on_a_missing_field():
    with pytest.raises(ValidationError, match="session_id"):
        CODEC.decode(ActionFrame, {"type": ActionFrame.type, "action": "run_tasks"})


def test_report_renders_one_error_as_path_and_message():
    with pytest.raises(ValidationError) as exc:
        CODEC.decode(ActionFrame, {"action": "run_tasks"})
    assert report(exc.value) == "session_id: Field required"


def test_report_joins_every_error_on_one_line():
    with pytest.raises(ValidationError) as exc:
        CODEC.decode(ActionFrame, {"session_id": 5, "action": ["x"]})
    text = report(exc.value)
    assert "\n" not in text
    assert "pydantic.dev" not in text
    assert text.startswith("session_id: Input should be a valid string, action: ")


def test_encode_round_trips_through_decode():
    envelope = ActionFrame(
        session_id="s-1", action="stop_batch", fields={"batch_uuid": "b"}
    )
    text = CODEC.encode(envelope)
    assert CODEC.decode(ActionFrame, text) == envelope


def test_the_adapter_cache_is_reused_per_class():
    _adapter.cache_clear()
    CODEC.decode(
        ActionFrame, {"type": ActionFrame.type, "session_id": "s", "action": "a"}
    )
    CODEC.decode(
        ActionFrame, {"type": ActionFrame.type, "session_id": "s", "action": "b"}
    )
    assert _adapter.cache_info().currsize == 1
