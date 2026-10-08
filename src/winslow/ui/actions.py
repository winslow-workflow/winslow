from enum import Enum, auto

from winslow.runner.execution import ExecutionStatus


SESSION_ENDING_MESSAGE = "Session is ending - new batches cannot be started"
NO_PERMISSION_MESSAGE = "No permission for this action on this workflow"

# The batch status names of a batch that still runs or waits. A stop request
# reaches only these. Derived from the enum, so a core rename cannot drift.
ACTIVE_BATCH_STATUSES = frozenset(
    status.name for status in (ExecutionStatus.QUEUED, ExecutionStatus.RUNNING)
)


class TaskActionEnum(Enum):
    RUN = auto()
    CHECK = auto()
    INFO = auto()
