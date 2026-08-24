from winslow.events import ExecutionStatusEvent, LogLineEvent, Origin, TaskStatusEvent
from winslow.store import BaseStore
from winslow.logger import LOGGER

from winslow.task.task import Task
from winslow.task.info import TaskInfo
from winslow.task.status import TaskStatus

from .execution import ExecutionRecord


def log_task_status(event: TaskStatusEvent):
    """Log one task status transition. A headless workflow subscribes this
    callback (see Workflow.__init__). The TUI shows each change in its
    widgets instead."""
    quiet = event.status is TaskStatus.INITIALIZED or event.origin is Origin.SEED
    func = LOGGER.debug if quiet else LOGGER.info
    func(f"Task {event.key} updated to {event.status}")


class TaskStore(BaseStore):
    item_class = Task
    status_class = TaskStatus


class ExecutionRecordStore(TaskStore):
    """Store of the task execution records for one batch. Its status writes are a
    copy of the main store. It thus publishes them as execution events, which are
    scoped to the batch, and not as live task status. The store outlives the
    batch as history, so it keys by the identity key and holds TaskInfo values,
    never a task (see release_tasks). It publishes on the session bus, so one
    subscription covers every batch, past and future."""

    item_class = str

    def __init__(self, bus, batch_uuid, items, root_dir=None):
        super().__init__(bus, items)
        self.batch_uuid = batch_uuid
        # The project root, so the sweep labels a source the same way as the
        # live detail view (see TaskInfo.from_task).
        self.root_dir = root_dir
        self._records: dict[str, ExecutionRecord] = {}

    def register(self, task):
        self._records[task.identity_key] = ExecutionRecord(
            info=TaskInfo.from_task(task), store=self
        )
        self[task.identity_key] = TaskStatus.READY_TO_PROCESS

    def capture(self, task):
        """Replace the stub of the task with a full capture. The sweep calls
        this outside every store write, which runs under the store lock."""
        record = self._records.get(task.identity_key)
        if record is not None:
            record.info = TaskInfo.from_task(task, full=True, root_dir=self.root_dir)

    def get_record(self, task_key) -> ExecutionRecord:
        return self._records[task_key]

    @property
    def records(self):
        return tuple(self._records.values())

    def _publish(self, task_key, status, origin):
        self.bus.publish(
            ExecutionStatusEvent(
                task_key=task_key,
                status=status,
                batch_uuid=self.batch_uuid,
                origin=origin,
            )
        )

    def emit_log_appended(self, task_key, line):
        self.bus.publish(
            LogLineEvent(task_key=task_key, batch_uuid=self.batch_uuid, line=line)
        )
