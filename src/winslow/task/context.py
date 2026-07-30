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

    - session_id:   identifies the full run, or session. It is the top-level
                    grouping key for "all logs of this execution". It is None
                    until a Session binds it to the runner, for example in a
                    headless run before a session exists.
    - workflow_name: a label with low cardinality, such as "alpha", to group the
                    runs of the same workflow. It is a separate field, although
                    it is also the prefix of session_id. The kebab name holds
                    hyphens, and the UUID holds hyphens too, so the name is not
                    cleanly recoverable from the id. It is also the dimension
                    that you index and filter the streams by.
    - task_name:    the true identity of the task, which is str(task): the class
                    name and the parameter values. Two parameterized tasks of the
                    same class are different tasks, so the parameters must be in
                    the label to separate their logs. It is None for a log record
                    of the run that has no task, for example the workflow init,
                    the task generation and the eligibility checks. Such a record
                    still needs the session_id and workflow_name labels, but it
                    has no task.
    - batch_uuid:   identifies the execution action, which is one run or check
                    that a user started and which can hold many tasks. With
                    task_name it identifies the execution of one task in the run.
                    It is None outside a batch.
    """

    session_id: Optional[str]
    workflow_name: Optional[str]
    task_name: Optional[str]
    batch_uuid: Optional[str]


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
