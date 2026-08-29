"""The Textual messages of the workflow screen. Every payload is a value
from the session port: an identity key, a status, a model dataclass (see
docs/ui-plugins.md). A pane never receives a live core object.

Every message is addressed to its pane directly (see _dispatch_to_slot), so
none bubbles: a bubbling message reaches every ancestor pump, and Textual
dispatches by handler name there."""

from textual.message import Message


class TaskStatusChanged(Message, bubble=False):
    """Carries the identity key of the task (see docs/ui-plugins.md)."""

    def __init__(self, key, status):
        self.key = key
        self.status = status
        super().__init__()


class BatchCreated(Message, bubble=False):
    """Carries the BatchInfo value of the created batch."""

    def __init__(self, info):
        self.info = info
        super().__init__()


class BatchCompleted(Message, bubble=False):
    """Carries the BatchInfo value of the completed batch."""

    def __init__(self, info):
        self.info = info
        super().__init__()


class ExecutionStatusChanged(Message, bubble=False):
    def __init__(self, batch_uuid, task_key, status):
        self.batch_uuid = batch_uuid
        self.task_key = task_key
        self.status = status
        super().__init__()


class TaskLogUpdated(Message, bubble=False):
    def __init__(self, batch_uuid, task_key, line):
        self.batch_uuid = batch_uuid
        self.task_key = task_key
        self.line = line
        super().__init__()


class TaskSelected(Message, bubble=False):
    def __init__(self, task_info):
        self.task_info = task_info
        super().__init__()


class SessionEnded(Message, bubble=False):
    """The session archived. A pane stops its live machinery, for example a
    refresh timer."""


class CacheUpdated(Message, bubble=False):
    """Any cache event. The pane repaints from a fresh caches read, so the
    message carries nothing."""


class CacheSelected(Message, bubble=False):
    """Carries the CacheCard value of the selected cache."""

    def __init__(self, card):
        self.card = card
        super().__init__()
