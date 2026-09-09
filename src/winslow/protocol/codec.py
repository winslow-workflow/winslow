"""A wrapper over pydantic TypeAdapter for the wire: a dataclass DTO to JSON
text and back. pydantic arrives with the [serve] or the [connect] extra, so
only the protocol, the wire client and the serve endpoints import this module."""

import functools
import json

from pydantic import TypeAdapter, ValidationError

__all__ = ["Codec", "CODEC", "ValidationError", "report"]


def report(exc):
    """One line for a ValidationError: the field path and the message of each
    error, joined by commas. 'session_id: Input should be a valid string'."""
    return ", ".join(_line(error) for error in exc.errors())


def _line(error):
    path = ".".join(str(part) for part in error["loc"])
    return f"{path}: {error['msg']}" if path else error["msg"]


@functools.cache
def _adapter(dto_class):
    """One TypeAdapter per class, shared by every thread: construction is the
    costly part, and an adapter is stateless after it."""
    return TypeAdapter(dto_class)


class Codec:
    """Encode a dataclass DTO to JSON text and decode JSON back into one (see
    winslow.model for the DTO classes)."""

    def encode(self, dto, **head):
        """The JSON text of one DTO instance. head adds the fields the class
        does not declare, like the type of a frame (see encode_frame)."""
        data = _adapter(type(dto)).dump_python(dto, mode="json")
        return json.dumps({**head, **data})

    def dump(self, value):
        """The JSON-ready form of one value: a dataclass becomes a dict, a
        tuple becomes a list, an enum becomes its value, a scalar passes."""
        return _adapter(object).dump_python(value, mode="json")

    def decode(self, dto_class, payload):
        """One instance of dto_class from a JSON dict or JSON text."""
        if isinstance(payload, (str, bytes)):
            return _adapter(dto_class).validate_json(payload)
        return _adapter(dto_class).validate_python(payload)


CODEC = Codec()
