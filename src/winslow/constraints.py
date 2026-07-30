"""Composable task constraints.

A constraint is a subclass of :class:`Constraint`, for the instance hooks, or of
:class:`ClassConstraint`, for should_be_initialized. Declare it in a list on the
task. A single constraint needs no list.

A constraint shares the gating logic by composition, so a base class is
unnecessary. Declare a list of constraints, or override the hook directly. Both
run.

The state that comes from the config is prepared at construction, because the
call of the hook receives only the task. A constraint for an instance hook is
called with ``(task)``. should_be_initialized runs at graph init, before an
instance exists, so its constraints are called with
``(task_class, parameters)``::

      class MinBalance(Constraint):
          valid_types = ConstraintType.RUNNABILITY
          def apply(self, task):
              return task.balance >= self.workflow_config.min_balance

      class EnvAllowed(ClassConstraint):
          valid_types = ConstraintType.INITIALIZATION
          def apply(self, task_class, parameters=None):
              return self.env in task_class.allowed_envs

A constraint can declare ``valid_types`` to limit the lists that can hold it. A
single type or a collection of types is legal. An empty collection, which is the
default, sets no limit. A constraint in a list that it is not valid for is
rejected when the constraints are resolved. A constraint with the wrong base
class for the lifecycle of the hook is also rejected.
"""

from enum import Enum, auto

from winslow import settings
from winslow.exceptions import MisconfigurationError
from winslow.util import to_tuple


class ConstraintType(Enum):
    # The task gate for each constraint list. INITIALIZATION is evaluated at
    # graph init, on the class (should_be_initialized), so its constraints take
    # (task_class, parameters). Each other type gates a live instance and takes
    # (task). CHECKABILITY gates the completion check and blocks the task if it
    # fails. SUCCESS adds to the success predicate (check).
    INITIALIZATION = auto()
    ELIGIBILITY = auto()
    RUNNABILITY = auto()
    CHECKABILITY = auto()
    SUCCESS = auto()


class _ConstraintMeta(type):
    """Give a clear error for the frequent mistake `[MyConstraint()]` in the place
    of `[MyConstraint]`. A constraint class is declared directly and the framework
    instantiates it with the config. A construction with no argument is thus an
    error. It occurs before the validation of Task can run, so this metaclass
    catches it."""

    def __call__(cls, *args, **kwargs):
        if not args and not kwargs:
            raise MisconfigurationError(
                f"{cls.__name__} is a constraint class - declare it directly as "
                f"`{cls.__name__}` (no parentheses); the framework initializes it "
                f"with config."
            )
        return super().__call__(*args, **kwargs)


class _ConstraintBase(metaclass=_ConstraintMeta):
    """The shared base of the constraint classes. Subclass :class:`Constraint` or
    :class:`ClassConstraint`, and not this class. The subclasses show the call
    signature."""

    # The constraint types that can hold this constraint. A single type or a
    # collection of types. An empty collection sets no limit.
    valid_types = frozenset()

    @classmethod
    def get_valid_types(cls):
        """The declared valid_types as a frozenset. A single type is legal,
        in the same way as Task.groups (see Task.get_groups)."""
        return frozenset(to_tuple(cls.valid_types))

    def __init__(self, workflow_config):
        self.workflow_config = workflow_config

    @property
    def env(self):
        return settings.env


class Constraint(_ConstraintBase):
    """Constraint for the instance hooks: is_eligible, can_run,
    can_check and check. It is called with the live task."""

    def __call__(self, task):
        return self.apply(task)

    def apply(self, task):
        raise NotImplementedError


class ClassConstraint(_ConstraintBase):
    """Constraint for should_be_initialized. It is evaluated at graph init, before
    an instance exists, and is called with the task class and the parameters."""

    def __call__(self, task_class, parameters=None):
        return self.apply(task_class, parameters)

    def apply(self, task_class, parameters=None):
        raise NotImplementedError
