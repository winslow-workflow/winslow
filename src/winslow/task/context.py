from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional


@dataclass
class BatchOptions:
    """Shared and mutable. The UI changes it live, and each batch takes a snapshot
    of it at its start. A later change thus does not affect a batch, and history
    shows the options that the batch used."""

    dry_run: bool
    force_run: bool
    force_success: bool
    disable_concurrency: bool


@dataclass
class TaskExecutionContext:
    batch_uuid: Optional[str]
    dry_run: bool
    force_run: bool
    force_success: bool
    disable_concurrency: bool


# A ContextVar and not a threading.local, so the context follows the logical
# flow. Each thread and each asyncio context starts with the default None.
#
# The failure with a threading.local, two tasks on one event loop thread:
#
#     task A sets its context   # the local holds the context of A
#     task B sets its context   # the local holds B, A is overwritten
#     task A reads the context  # A sees the batch flags of B
#
# A threading.local also does not follow asyncio.to_thread: the worker
# thread starts empty, so a transient_property in the offloaded code
# raises RuntimeError (see TransientProperty).
#
# A ContextVar resolves both problems. Each asyncio Task runs in its own
# copy of the context, so the write of B does not touch A. And
# asyncio.to_thread copies the context into the worker thread, so the
# offloaded code reads the context of its own task.
_task_execution: ContextVar[Optional[TaskExecutionContext]] = ContextVar(
    "task_execution_context", default=None
)


def get_execution_context() -> Optional[TaskExecutionContext]:
    return _task_execution.get()


def set_execution_context(context: Optional[TaskExecutionContext]):
    _task_execution.set(context)


@contextmanager
def scoped_execution_context(context: TaskExecutionContext):
    if get_execution_context() is not None:
        raise RuntimeError(
            "Nested execution contexts are not allowed in the current context"
        )
    token = _task_execution.set(context)
    try:
        yield context
    finally:
        _task_execution.reset(token)


@dataclass
class LogContext:
    """Structured labels that are stamped onto each log record from the scope of a
    task. A sink, such as a file or Loki, thus gets metadata that it can query,
    and it does not parse the message. Together the labels answer the question
    "which task execution wrote this line?". Each field is a plain scalar,
    because a log record goes to a remote sink. The record carries values and
    never a live object.

    Each axis carries the declared name and the instance. The name is bounded,
    so a sink can index and group by it; the instance is the display form with
    the identifying values, so a filter can select one parameter row or one
    configured run:

    - session_id:   identifies one execution, the top-level grouping key. None
                    until a Session binds it to the runner.
    - workflow_name: the declared name, for example "etl".
    - workflow_instance: str(workflow), for example "etl (client=acme)". Two
                    configured runs of one workflow differ by it.
    - task_name:    the declared name, for example "deploy". None for a record
                    outside a task, for example the workflow init and the
                    eligibility checks.
    - task_instance: str(task), the declared name plus the parameter values.
    - batch_uuid:   identifies the execution action, one run or check that a
                    user started. None outside a batch.
    - task_uuid:    the uuid of the task. It is not stamped onto the records:
                    an ambient producer reads it to build the routing adapter
                    of the task (see winslow.cache.cache_logger).
    """

    session_id: Optional[str]
    workflow_name: Optional[str]
    workflow_instance: Optional[str]
    task_name: Optional[str]
    task_instance: Optional[str]
    batch_uuid: Optional[str]
    task_uuid: Optional[str] = None


_log_context: ContextVar[Optional[LogContext]] = ContextVar("log_context", default=None)


def get_log_context() -> Optional[LogContext]:
    return _log_context.get()


@contextmanager
def scoped_log_context(context: LogContext):
    token = _log_context.set(context)
    try:
        yield context
    finally:
        _log_context.reset(token)
