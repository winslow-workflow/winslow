from enum import Enum, auto


SESSION_ENDING_MESSAGE = "Session is ending - new batches cannot be started"


class TaskActionEnum(Enum):
    RUN = auto()
    CHECK = auto()
    INFO = auto()
