"""The serve edge serializer: a winslow-style wrapper over pydantic
TypeAdapter. Exactly two consumers reach this module: the serve transports
and a future websocket client transport. Core, headless and the local TUI
never import it. A serve process with no [serve] extra fails this import,
and the caller turns that into a directional MisconfigurationError (see
winslow.orchestrator._handle_serve and ServeApp._build_mcp, the same
pattern this module rides)."""

from pydantic import TypeAdapter, ValidationError

__all__ = ["Codec", "CODEC", "ValidationError"]


class Codec:
    """Encode a dataclass DTO to JSON text and decode JSON back into one. One
    TypeAdapter per class: construction is the costly part, so an instance
    caches it (see winslow.model for the DTO classes)."""

    def __init__(self):
        self._adapters = {}

    def _adapter(self, dto_class):
        adapter = self._adapters.get(dto_class)
        if adapter is None:
            adapter = TypeAdapter(dto_class)
            self._adapters[dto_class] = adapter
        return adapter

    def encode(self, dto):
        """The JSON text of one DTO instance."""
        return self._adapter(type(dto)).dump_json(dto).decode("utf-8")

    def decode(self, dto_class, payload):
        """One instance of dto_class from a JSON-shaped dict or JSON text.
        Raises pydantic.ValidationError with a directional message on a
        malformed payload."""
        if isinstance(payload, (str, bytes)):
            return self._adapter(dto_class).validate_json(payload)
        return self._adapter(dto_class).validate_python(payload)


# One codec per process: the adapter cache is safe to share, because a
# TypeAdapter is stateless after construction.
CODEC = Codec()
