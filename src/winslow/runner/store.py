from winslow.store import BaseStore, StoreListener
from winslow.logger import LOGGER

from winslow.task.task import Task
from winslow.task.info import TaskInfo
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
    because the main store already logs each status. The store outlives the
    batch as history, so it keys by the task uuid and holds TaskInfo values,
    never a task (see release_tasks)."""

    item_class = str

    def __init__(self, batch_uuid, items, root_dir=None):
        super().__init__(items)
        self.batch_uuid = batch_uuid
        # The project root, so the sweep labels a source the same way as the
        # live detail view (see TaskInfo.from_task).
        self.root_dir = root_dir
        self._records: dict[str, ExecutionRecord] = {}

    def register(self, task):
        self._records[task.uuid] = ExecutionRecord(
            info=TaskInfo.from_task(task), store=self
        )
        self[task.uuid] = TaskStatus.READY_TO_PROCESS

    def capture(self, task):
        """Replace the stub of the task with a full capture. The sweep calls
        this outside every store write, which runs under the store lock."""
        record = self._records.get(task.uuid)
        if record is not None:
            record.info = TaskInfo.from_task(task, full=True, root_dir=self.root_dir)

    def get_record(self, task_uuid) -> ExecutionRecord:
        return self._records[task_uuid]

    @property
    def records(self):
        return tuple(self._records.values())

    def _emit_status(self, task_uuid, status):
        self._emit(
            StoreListener.on_execution_status, task_uuid, status, self.batch_uuid
        )

    def emit_log_appended(self, task_uuid, line):
        self._emit(StoreListener.on_log_appended, task_uuid, self.batch_uuid, line)
