import collections
import logging
import weakref
from functools import cached_property

from winslow.exceptions import MisconfigurationError
from winslow.constraints import (
    ConstraintType,
    Constraint,
    ClassConstraint,
    _ConstraintBase,
)
from winslow.util import to_tuple, safe_repr, flatten
from winslow.logger import TASK_LOGGER_NAME, get_task_dispatcher
from winslow.settings import TASK_LOG_BUFFER_SIZE
from winslow._parameterization import (
    _ParameterizationBase,
    _GetParametersNotImplemented,
)
from winslow.task.context import get_execution_context
from winslow import exceptions
from winslow.exceptions import TaskActionRequired

from .info import TaskInfo


# Instance-level constraint gates -> the attribute that holds each list. These
# constraints are evaluated per task instance and take (task). This is framework
# wiring and not task API, so it stays at module level and not on the class.
_INSTANCE_CONSTRAINTS = {
    ConstraintType.ELIGIBILITY: "eligibility_constraints",
    ConstraintType.RUNNABILITY: "runnability_constraints",
    ConstraintType.CHECKABILITY: "checkability_constraints",
    ConstraintType.SUCCESS: "success_constraints",
}

# Class-level gates. The graph evaluates them at init, before an instance exists.
# These constraints take (task_class, parameters). The ctype -> attribute dict
# keeps symmetry with _INSTANCE_CONSTRAINTS. Only the membership is read today.
_CLASS_CONSTRAINTS = {
    ConstraintType.INITIALIZATION: "initialization_constraints",
}


class _ExecutionFlag:
    """Batch-scoped flag from the active execution context. This is a descriptor
    and not a property, so the attributes view drops it. Like transient_property,
    it has no value outside a batch."""

    def __init__(self, flag):
        self._flag = flag

    def __set_name__(self, owner, name):
        self._name = name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        return getattr(obj._active_execution_context, self._flag)

    def __set__(self, obj, value):
        raise AttributeError(f"'{self._name}' is read-only.")


class Task(_ParameterizationBase):
    class Meta:
        abstract = True

    # The name attribute comes from _Base.
    groups = None

    # Examples of dependency declarations:
    # dependencies = MyTask
    # dependencies = (MyTask1, "MyTask2")
    # dependencies = "MyTask"

    dependencies = None

    # A premier task runs before all tasks that are not premier. A premier task
    # is a foundation step with no explicit dependencies downstream.
    is_premier = False

    # A terminal task runs last, independent of its priority.
    is_terminal = False

    # Tasks in the same priority group are processed concurrently. Set these to
    # False to prevent the run or the completion check from overlapping other
    # work in the batch.
    can_run_parallel = True
    can_check_parallel = True

    # Composable constraints: the Constraint classes that gate the matching
    # hook (see winslow.constraints). A single class or a collection. None
    # means that no constraint is declared. Declare these constraints or
    # override the hook directly. Both run when the gate is evaluated.
    eligibility_constraints = None
    runnability_constraints = None
    checkability_constraints = None
    success_constraints = None
    initialization_constraints = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # True when the task defines success. _evaluate_check reads this flag
        # and never calls the default check, which raises NotImplementedError.
        cls._check_overridden = cls.check is not Task.check

    @classmethod
    def _validate_constraint(cls, c, ctype):
        # One validation flow for every constraint that the get_*_constraints
        # methods return, applied at resolve time. The base class follows the
        # lifecycle: an instance hook takes Constraint, and graph init
        # (should_be_initialized) takes ClassConstraint.
        expected = ClassConstraint if ctype in _CLASS_CONSTRAINTS else Constraint
        if isinstance(c, _ConstraintBase):
            raise MisconfigurationError(
                f"{cls.__name__}: {ctype.name} constraint declared as an instance "
                f"({c!r}) - pass the class; the framework initializes it."
            )
        if not (isinstance(c, type) and issubclass(c, expected)):
            raise MisconfigurationError(
                f"{cls.__name__}: {ctype.name} constraint {c!r} must be a "
                f"{expected.__name__} subclass."
            )
        valid_types = c.get_valid_types()
        if valid_types and ctype not in valid_types:
            valid_names = ", ".join(t.name for t in valid_types)
            raise MisconfigurationError(
                f"{cls.__name__}: {c.__name__!r} is not valid as a {ctype.name} "
                f"constraint (valid_types=[{valid_names}])."
            )

    def __init__(self, workflow_config, parameters=None):

        super().__init__(parameters=parameters)

        self.workflow_config = workflow_config

        if self.is_premier and self.is_terminal:
            raise MisconfigurationError(
                f"{self} cannot be declared as premier and terminal at the same time."
            )

        # The graph sets these after it initializes the tasks.
        self._index = None
        self._dependent_tasks = None
        self._priority = None
        self._workflow_name = None

        # All tasks share one logger. The adapter stamps the task uuid onto
        # each record, so the dispatcher and a log view route by the uuid
        # alone. Records propagate to the winslow.runs sink.
        self.logger = logging.LoggerAdapter(
            logging.getLogger(TASK_LOGGER_NAME), {"task_id": self.uuid}
        )

        self._log_buffer = None

        # Local flag that marks the task as eligible or ineligible. The graph can
        # set it while it assigns the dependencies. The runner then does not
        # check the eligibility again.
        self._is_eligible_result = None

    def _enable_log_buffer(self):
        """Buffer the log records of this task for the Logs tab of the info
        modal. The graph calls this at setup time, in interactive mode only. The
        finalizer drops the buffer when the task is collected. A released task
        therefore frees its buffer, and a task that history retains keeps it."""
        self._log_buffer = collections.deque(maxlen=TASK_LOG_BUFFER_SIZE)
        dispatcher = get_task_dispatcher()
        dispatcher.register_buffer(self.uuid, self._log_buffer)
        weakref.finalize(self, dispatcher.unregister, self.uuid)

    @property
    def buffered_logs(self):
        """Buffered log records for the Logs tab of the info modal. None when the
        task is not interactive."""
        return self._log_buffer

    @property
    def _execution_context(self):
        return get_execution_context()

    @property
    def _active_execution_context(self):
        ctx = get_execution_context()
        if ctx is None:
            raise RuntimeError(
                f"Execution flags for {self} are only available during batch "
                f"execution (run/check) - not at import, eligibility or "
                f"graph-build time."
            )
        return ctx

    is_dry_run = _ExecutionFlag("dry_run")
    is_force_run = _ExecutionFlag("force_run")
    is_force_success = _ExecutionFlag("force_success")

    # True after run() of this instance started at least once, in any batch
    # of this workflow. dry_run does not set it. The completion check uses
    # it to separate COMPLETED from COMPLETED_PREVIOUSLY.
    _has_been_run = False

    @property
    def is_noop(self):
        """True for a task with no run action, which only checks completion. The
        value is inferred from run, because the base run does nothing. It thus
        inherits without a declaration: if any class in the MRO overrides run,
        the task is no longer noop."""
        return type(self).run is Task.run

    @property
    def can_process_parallel(self):
        # Processing includes the checks and the run, so it needs both gates.
        # A noop task only checks.
        if self.is_noop:
            return self.can_check_parallel
        return self.can_run_parallel and self.can_check_parallel

    @classmethod
    def get_parameters(cls, workflow_config):

        if not cls._is_parameterized:
            raise ValueError(
                "There is no point trying to get parameters for non-parameterized tasks."
            )

        raise _GetParametersNotImplemented(
            "get_parameters should be explicitly overridden for parametrized tasks to return a"
            " list of dictionaries that match the parameterization fields."
        )

    @classmethod
    def get_groups(cls):
        groups = cls.groups if cls.groups else []
        return frozenset(to_tuple(groups))

    @property
    def groups_readable(self):
        return ", ".join(sorted(self.get_groups()))

    @cached_property
    def _str_cached(self):

        label = str(self.instance_name)

        if self._is_parameterized:
            param_values_readable = ", ".join(
                safe_repr(v) for v in self._parameters_dict.values()
            )

            label = f"{label} ({param_values_readable})"

        return label

    def __str__(self):
        return self._str_cached

    @property
    def dependent_tasks(self):
        return self._dependent_tasks or tuple()

    @property
    def info(self):
        return TaskInfo.from_task(self)

    @classmethod
    def _get_dependency_context(cls):
        """Return the full dependency context of the task in flat form."""
        dependencies = cls.dependencies

        if not dependencies:
            return tuple()

        return flatten(to_tuple(dependencies))

    @property
    def priority(self):
        """A lower priority is processed first. The graph stamps this value with
        the topological generation of the task. This property does not
        calculate it."""
        if self._priority is None:
            raise MisconfigurationError(
                f"{self} priority read before the graph assigned it."
            )
        return self._priority

    @property
    def execution_order(self):
        return (not self.is_premier, self.is_terminal, self.priority)

    @classmethod
    def should_be_initialized(cls, workflow_config, parameters=None):
        """
        Override this to prevent the creation of the task instance.

        This is stronger than is_eligible, which filters the tasks after their
        creation. Use it with task parameterization when most of the
        parameterized tasks are skipped.

        A task that is not initialized does not show on the UI. This can confuse
        a user who declared the task.
        """
        return True

    @classmethod
    def get_initialization_constraints(cls, workflow_config):
        """Override this to select should_be_initialized constraints by env or by
        config."""
        return cls.initialization_constraints

    @classmethod
    def _evaluate_should_be_initialized(cls, workflow_config, parameters=None):
        # Class-level constraints take (task_class, parameters). The get_* method
        # supplies them. They go through the same validate and resolve flow, and
        # they run with the override.
        for c in to_tuple(cls.get_initialization_constraints(workflow_config) or ()):
            if not cls._prepare_constraint(
                c, ConstraintType.INITIALIZATION, workflow_config
            )(cls, parameters):
                return False
        return cls.should_be_initialized(workflow_config, parameters=parameters)

    def depends_on(self, task):
        """
        Override this to control if a task instance depends on another task
        instance.

        Use it to prevent a cyclic dependency between two task classes that
        depend on each other. Parameterization makes this more frequent.
        """
        return True

    def is_eligible(self):
        """Override this to control the eligibility of the task for the workflow."""
        return True

    def can_run(self):
        """Override this to block a task that does not satisfy your constraints."""
        return True

    def can_check(self):
        """Override this to gate the completion check. Return False, or call
        self.block, to mark the task BLOCKED and not run check. Use it when the
        check depends on data that is not always available."""
        return True

    def check(self):
        """Override this to implement the success check."""
        raise NotImplementedError

    # -- constraint evaluation -------------------------------------------------
    # The runner calls the sealed _evaluate_* methods and never the hooks. The
    # declared constraints thus always run with an override. The constraints run
    # first, so a cheap declarative guard can stop the evaluation before the
    # custom logic.

    # The get_*_constraints methods return the constraints for each gate. The
    # default is the declared class attribute. Override them to select the
    # constraints by env or by self.workflow_config.
    def get_eligibility_constraints(self):
        return self.eligibility_constraints

    def get_runnability_constraints(self):
        return self.runnability_constraints

    def get_checkability_constraints(self):
        return self.checkability_constraints

    def get_success_constraints(self):
        return self.success_constraints

    @cached_property
    def _constraint_instances(self):
        """Resolve the instance-level constraints one time, from the
        get_*_constraints methods. Validate each constraint, then instantiate
        it."""
        return {
            ctype: tuple(
                self._prepare_constraint(c, ctype, self.workflow_config)
                for c in to_tuple(getattr(self, f"get_{attr}")() or ())
            )
            for ctype, attr in _INSTANCE_CONSTRAINTS.items()
        }

    @classmethod
    def _prepare_constraint(cls, c, ctype, workflow_config):
        """Validate the constraint, then instantiate it. Every constraint from
        get_*_constraints goes through this one step."""
        cls._validate_constraint(c, ctype)
        return c(workflow_config)

    def _check_constraints(self, ctype):
        return all(c(self) for c in self._constraint_instances[ctype])

    def _evaluate_is_eligible(self):
        return (
            self._check_constraints(ConstraintType.ELIGIBILITY) and self.is_eligible()
        )

    def _evaluate_can_run(self):
        return self._check_constraints(ConstraintType.RUNNABILITY) and self.can_run()

    def _evaluate_can_check(self):
        return self._check_constraints(ConstraintType.CHECKABILITY) and self.can_check()

    def _evaluate_check(self):
        constraints = self._constraint_instances[ConstraintType.SUCCESS]
        # The task must define success. If it does not, all([]) reports completed
        # with no check. This runs here and not at import, because
        # get_success_constraints supplies the constraints and can depend on the
        # env.
        if not self._check_overridden and not constraints:
            raise MisconfigurationError(
                f"{type(self).__name__} must define success: override check() "
                f"or provide success_constraints."
            )
        result = all(c(self) for c in constraints)
        if self._check_overridden:
            result = result and self.check()
        return result

    def run(self):
        """Make the system change. The check method then confirms the change."""
        pass

    def dry_run(self):
        """The runner calls this instead of run in dry-run mode. Override it to
        simulate the change. The default makes no change."""
        pass

    def block(self, msg):
        raise exceptions.TaskBlock(msg)

    def skip(self, msg):
        raise exceptions.TaskSkip(msg)

    def fail(self, msg):
        raise exceptions.TaskFailure(msg)

    def require_action(self, msg):
        raise TaskActionRequired(msg)

    def _check_and_raise(self, condition, msg, method):
        result = condition() if callable(condition) else condition

        if result:
            method(msg)

    def block_if(self, condition, msg):
        self._check_and_raise(condition, msg, self.block)

    def skip_if(self, condition, msg):
        self._check_and_raise(condition, msg, self.skip)

    def fail_if(self, condition, msg):
        self._check_and_raise(condition, msg, self.fail)
