from winslow.task import TaskStatus
from winslow.runner.execution import ExecutionStatus

DEFAULT_ICON = "❓"

TASK_ICONS = {
    TaskStatus.INITIALIZED: "⚪",
    TaskStatus.CHECKING_ELIGIBILITY: "🟠",
    TaskStatus.SKIPPED: "🔵",
    TaskStatus.READY_TO_PROCESS: "🟡",
    TaskStatus.READY_TO_RUN: "🟡",
    TaskStatus.CHECKING_RUNNABILITY: "🟠",
    TaskStatus.RUNNING: "🟠",
    TaskStatus.RUN_FINISHED: "🟠",
    TaskStatus.FAILED: "🔴",
    TaskStatus.ERROR: "❗",
    TaskStatus.BLOCKED: "🚫",
    TaskStatus.CHECKING_DEPENDENCIES: "🟠",
    TaskStatus.CHECKING_COMPLETION: "🟠",
    TaskStatus.ACTION_REQUIRED: "✋",
    TaskStatus.COMPLETED: "🟢",
    TaskStatus.COMPLETED_WITH_ERROR: "🟣",
    TaskStatus.COMPLETED_PREVIOUSLY: "🟢",
    TaskStatus.FORCE_SUCCESS: "🟢",
    TaskStatus.STALE: "🔴",
    TaskStatus.ABORTED: "⛔",
    TaskStatus.WAITING_FOR_RELEASE: "⏳",
}


EXECUTION_ICONS = {
    ExecutionStatus.QUEUED: "⏳",
    ExecutionStatus.RUNNING: "🟠",
    ExecutionStatus.FINISHED: "🟢",
    ExecutionStatus.STOPPED: "⛔",
    ExecutionStatus.ERRORED: "❗",
}


def get_task_icon(status=None):
    return TASK_ICONS.get(status, DEFAULT_ICON)


def get_execution_icon(status=None):
    return EXECUTION_ICONS.get(status, DEFAULT_ICON)
