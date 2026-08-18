from winslow.ui.plugin import Slots, TaskDetailRenderContext

from .common import BaseModal

SLOT = Slots.TASK_DETAIL


class TaskDetail(BaseModal):
    def __init__(
        self,
        info,
        registry,
        logs=None,
        transient_snapshots=None,
        cache_snapshots=None,
        *args,
        **kwargs,
    ):
        self.w_info = info
        self.registry = registry
        self._logs = logs
        self._transient_snapshots = transient_snapshots
        self._cache_snapshots = cache_snapshots
        super().__init__(*args, **kwargs)

    @property
    def modal_title(self):
        return str(self.w_info)

    def compose_content(self):
        context = TaskDetailRenderContext(
            info=self.w_info,
            logs=self._logs,
            transient_snapshots=self._transient_snapshots,
            cache_snapshots=self._cache_snapshots,
        )
        yield from self.registry.compose_slot(SLOT, context)
