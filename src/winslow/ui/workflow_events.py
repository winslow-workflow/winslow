from textual.message import Message


class TaskStatusChanged(Message):
    """Carries the identity key of the task (see docs/ui-plugins.md)."""

    BUBBLE = False

    def __init__(self, key, status):
        self.key = key
        self.status = status
        super().__init__()


class BatchCreated(Message):
    BUBBLE = False

    def __init__(self, batch):
        self.batch = batch
        super().__init__()


class BatchCompleted(Message):
    BUBBLE = False

    def __init__(self, batch):
        self.batch = batch
        super().__init__()


class ExecutionStatusChanged(Message):
    BUBBLE = False

    def __init__(self, batch, task_key, status):
        self.batch = batch
        self.task_key = task_key
        self.status = status
        super().__init__()


class TaskLogUpdated(Message):
    BUBBLE = False

    def __init__(self, batch, task_key, line):
        self.batch = batch
        self.task_key = task_key
        self.line = line
        super().__init__()


class TaskSelected(Message):
    BUBBLE = False

    def __init__(self, task_info):
        self.task_info = task_info
        super().__init__()


class CacheUpdated(Message):
    """Any cache event. The pane repaints from a fresh peek of the cache
    itself, so the message carries nothing."""

    BUBBLE = False


class CacheSelected(Message):
    BUBBLE = False

    def __init__(self, cache):
        self.cache = cache
        super().__init__()
