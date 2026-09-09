"""winslow.codec: one TypeAdapter per DTO class, cached, encoding and
decoding the model dataclasses of winslow.model."""

import pytest

from winslow.codec import CODEC, Codec, ValidationError
from winslow.model import ActionFrame, ApplyFilterRequest


def test_decode_builds_the_dataclass_from_a_dict():
    envelope = CODEC.decode(
        ActionFrame,
        {
            "type": "action",
            "session_id": "s-1",
            "action": "run_tasks",
            "request_id": "r-1",
            "fields": {"keys": ["a", "b"]},
        },
    )
    assert envelope == ActionFrame(
        type="action",
        session_id="s-1",
        action="run_tasks",
        request_id="r-1",
        fields={"keys": ["a", "b"]},
    )


def test_decode_fills_declared_defaults():
    envelope = CODEC.decode(
        ApplyFilterRequest,
        {"type": "request", "kind": "apply_filter", "session_id": "s-1", "query": "a"},
    )
    assert envelope.request_id is None
    assert envelope.builtin_only is False


def test_decode_raises_with_a_directional_message_on_a_missing_field():
    with pytest.raises(ValidationError, match="session_id"):
        CODEC.decode(ActionFrame, {"type": "action", "action": "run_tasks"})


def test_encode_round_trips_through_decode():
    envelope = ActionFrame(
        type="action", session_id="s-1", action="stop_batch", fields={"batch_uuid": "b"}
    )
    text = CODEC.encode(envelope)
    assert CODEC.decode(ActionFrame, text) == envelope


def test_the_adapter_cache_is_reused_per_class():
    codec = Codec()
    assert len(codec._adapters) == 0
    codec.decode(ActionFrame, {"type": "action", "session_id": "s", "action": "a"})
    codec.decode(ActionFrame, {"type": "action", "session_id": "s", "action": "b"})
    assert len(codec._adapters) == 1
