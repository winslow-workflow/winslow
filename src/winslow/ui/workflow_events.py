from textual.message import Message


class TaskStatusChanged(Message):
    BUBBLE = False

    def __init__(self, task, status):
        self.task = task
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

    def __init__(self, batch, task_uuid, status):
        self.batch = batch
        self.task_uuid = task_uuid
        self.status = status
        super().__init__()


class TaskLogUpdated(Message):
    BUBBLE = False

    def __init__(self, batch, task_uuid, line):
        self.batch = batch
        self.task_uuid = task_uuid
        self.line = line
        super().__init__()


class TaskSelected(Message):
    BUBBLE = False

    def __init__(self, task_info):
        self.task_info = task_info
        super().__init__()
