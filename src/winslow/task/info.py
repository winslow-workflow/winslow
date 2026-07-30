import inspect
import os
from functools import lru_cache

from dataclasses import dataclass

from winslow.util import safe_repr

# The types that Pretty renders directly. Any other value becomes a safe
# string, because the repr of an unknown object can raise or be huge.
_PLAIN_TYPES = (str, int, float, bool, type(None))


def _display_parameters(task):
    return {
        name: value if isinstance(value, _PLAIN_TYPES) else safe_repr(value)
        for name, value in task._parameters_dict.items()
    }


@lru_cache(maxsize=None)
def _safe_source(cls):
    """The source of a class, or None if it has none, such as object or a C-level
    struct. The result is cached by class, because inspect.getsource reads the
    source file."""
    try:
        return inspect.getsource(cls)
    except (OSError, TypeError):
        return None


def _safe_sourcefile(cls):
    """The absolute path of the file that declares a class, or None if there is
    none. It uses the real __file__ through inspect, so it does not use the
    synthetic scoped module name. It also works for a base class, which the
    registry does not collect."""
    try:
        path = inspect.getsourcefile(cls)
    except TypeError:
        return None
    return os.path.abspath(path) if path else None


@dataclass(frozen=True)
class SourceNode:
    """A node in the inheritance source tree of a task."""

    name: str
    module: str
    source: str
    path: str  # absolute source file, or None
    children: tuple  # tuple[SourceNode, ...]


@lru_cache(maxsize=None)
def _source_tree(cls):
    """The inheritance source tree of a class. The cache is at module level and
    the key is the class, so 200 instances of one parameterized task build it one
    time. The recursion also shares a cached subtree between task classes, for
    example the Task leaf.

    The function walks __bases__ and skips object and each C-level class, which
    has no source. It stops at the Task base. Task is a node in the tree, but its
    framework bases are not."""
    from winslow.task.task import Task

    children = ()
    if cls is not Task:  # Task is the boundary for internal classes
        children = tuple(
            _source_tree(base)
            for base in cls.__bases__
            if base is not object and _safe_source(base) is not None
        )
    return SourceNode(
        name=cls.__name__,
        module=cls.__module__,
        source=_safe_source(cls) or "",
        path=_safe_sourcefile(cls),
        children=children,
    )


@dataclass(frozen=True)
class TaskInfo:
    task: object
    label: str
    is_premier: bool
    is_terminal: bool
    is_noop: bool
    task_class: str
    index: int
    groups: str = None
    parameters: dict = None
    dependencies: tuple = None
    premier_dependencies: tuple = None
    terminal_dependencies: tuple = None

    def __str__(self):
        return self.label

    @classmethod
    def from_task(cls, task):
        deps = task.dependent_tasks
        task_cls = task.__class__

        return cls(
            task=task,
            label=str(task),
            is_terminal=task.is_terminal,
            is_premier=task.is_premier,
            is_noop=task.is_noop,
            index=task._index,
            # The real source file through inspect, and not the synthetic scoped
            # __module__.
            task_class=f"{task_cls.__qualname__} ({_safe_sourcefile(task_cls) or task_cls.__module__})",
            groups=task.groups_readable or None,
            parameters=_display_parameters(task) or None,
            dependencies=[d for d in deps if not (d.is_premier or d.is_terminal)],
            premier_dependencies=[d for d in deps if d.is_premier],
            terminal_dependencies=[d for d in deps if d.is_terminal],
        )

    def source_tree(self):
        """The inheritance source tree with the concrete task class as its root
        (see _source_tree at module level). The cache is per class and not per
        instance."""
        return _source_tree(type(self.task))
