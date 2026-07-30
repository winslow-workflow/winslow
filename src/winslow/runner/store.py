from winslow.store import BaseStore
from winslow.logger import LOGGER

from winslow.task.task import Task
from winslow.task.status import TaskStatus

from .execution import ExecutionRecord


class TaskStore(BaseStore):
    item_class = Task
    status_class = TaskStatus

    def callback(self, task, status):
        func = LOGGER.debug if status is TaskStatus.INITIALIZED else LOGGER.info
        func(f"Task {task} updated to {status}")


class InteractiveStore(TaskStore):
    """No status logging. The widgets of the UI already show each change."""

    def callback(self, task, status):
        pass


class ExecutionRecordStore(InteractiveStore):
    """Store of the task execution records for one batch. Its status writes are a
    copy of the main store. It thus sends them as execution events, which are
    scoped to the batch, and not as live task status. It writes no log line,
    because the main store already logs each status."""

    def __init__(self, batch_uuid, items):
        super().__init__(items)
        self.batch_uuid = batch_uuid
        self._records: dict[Task, ExecutionRecord] = {}

    def register(self, task):
        self._records[task] = ExecutionRecord(task=task, store=self)
        self[task] = TaskStatus.READY_TO_PROCESS

    def get_record(self, task) -> ExecutionRecord:
        return self._records[task]

    def _emit_status(self, task, status):
        for listener in self._listeners:
            listener.on_execution_status(task, status, self.batch_uuid)

    def emit_log_appended(self, task, line):
        for listener in self._listeners:
            listener.on_log_appended(task, self.batch_uuid, line)
