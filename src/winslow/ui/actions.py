from enum import Enum, auto


SESSION_ENDING_MESSAGE = "Session is ending - new batches cannot be started"

# The batch status names of a batch that still runs or waits. A stop request
# reaches only these (see ExecutionStatus).
ACTIVE_BATCH_STATUSES = frozenset(("QUEUED", "RUNNING"))


class TaskActionEnum(Enum):
    RUN = auto()
    CHECK = auto()
    INFO = auto()
