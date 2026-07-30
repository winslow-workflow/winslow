from winslow.ui.plugin import Slots, TaskDetailRenderContext

from .common import BaseModal

SLOT = Slots.TASK_DETAIL


class TaskDetail(BaseModal):
    def __init__(
        self, task, registry, logs=None, transient_snapshots=None, *args, **kwargs
    ):
        self.w_task = task
        self.registry = registry
        self._logs = logs
        self._transient_snapshots = transient_snapshots
        super().__init__(*args, **kwargs)

    @property
    def modal_title(self):
        return str(self.w_task)

    def compose_content(self):
        context = TaskDetailRenderContext(
            task=self.w_task,
            logs=self._logs,
            transient_snapshots=self._transient_snapshots,
        )
        yield from self.registry.compose_slot(SLOT, context)
