class WinslowException(Exception):
    """The root of each exception that winslow raises intentionally."""


class WinslowError(WinslowException):
    """A defect or an unexpected outcome. The clean-error path of the CLI catches
    this and exits with a message."""


class TaskSignal(WinslowException):
    """Flow control. A task hook raises it and the status handlers of the runner
    consume it. A signal that escapes the runner is a defect and stops the
    program. The CLI does not handle it."""


class MisconfigurationError(WinslowError):
    pass


class InitializationError(WinslowError):
    """A workflow did not start. Its should_be_initialized gate refused, or an
    initialization invariant is broken. This is a sibling of
    MisconfigurationError, which reports bad input, and not a subclass. A caller
    can thus catch "does not initialize" and "the input is wrong" separately."""


class EligibilityError(WinslowError):
    """The is_eligible hook of a task raised an error. Eligibility selects the
    tasks that run, so a gate with a defect aborts the run. The framework does not
    estimate the shape of the workflow."""


class SessionEndingError(WinslowError):
    """A batch was refused, because its session accepts no more work. The session
    is ending or has ended, and the store is released or is about to be
    released."""


class ParameterizationError(WinslowError):
    def __init__(self, msg, task_kls=None):
        if task_kls:
            msg = f"Parameterization failure for class {task_kls.__name__}: " + msg
        super().__init__(msg)


class RegistrationError(WinslowError):
    pass


class IdentityKeyCollisionError(WinslowError):
    """Two live tasks resolve to one identity key (see TaskIndex). The key
    digests the parameter reprs, so two parameter sets with one repr collide."""


class PluginError(WinslowError):
    """A plugin or a filter is wrong: a clash of a command or a name, a replace
    that is not possible, or enable and disable lists that contradict each other.
    This is different from MisconfigurationError, which reports a setup problem
    with no relation to an extension."""


class CyclicalDependencyError(WinslowError):
    def __init__(self, tasks):

        # Add the first item at the end, to make the message more clear.
        if tasks[0] != tasks[-1]:
            tasks = tasks + [tasks[0]]

        ctx = " ==> ".join(repr(task) for task in tasks)
        error_msg = f"Cyclical dependency found: {ctx}"
        super().__init__(error_msg)


class CacheReentrancyError(WinslowError):
    """A cache loader re-entered a field that its own thread computes: an
    undeclared read cycle or a loader invalidation, which would deadlock."""


class SerializationError(WinslowError):
    """A storage backend cannot serialize a value. The write is strict on
    purpose: a silent coercion would make a persisted record lossy."""


class DeserializationError(WinslowError):
    """A storage backend cannot decode a stored record. JsonFileStorage catches
    its own and serves a cold miss; a custom layer can let it propagate."""


class StorageError(WinslowError):
    """A storage operation failed on one or more tiers. `tiers` carries the
    label of each failing tier (see ComposedStorage)."""

    def __init__(self, message, tiers=None):
        super().__init__(message)
        self.tiers = tuple(tiers) if tiers else ()


class IllegalTaskOutcomeError(WinslowError):
    """A task hook raised a TaskSignal that is not legal for the step that it ran
    in, for example skip outside eligibility. It is raised on the reraise_errors
    path, so what escapes is a defect. A signal handler upstream must never accept
    it as legal flow control."""


class TaskSkip(TaskSignal):
    """
    Raise this to mark a task as ineligible for the current workflow and config.

    Shortcut: task.skip and task.skip_if.
    """


class TaskBlock(TaskSignal):
    """
    Raise this when a procedure cannot run because of a constraint.

    Shortcut: task.block and task.block_if.
    """


class TaskFailure(TaskSignal):
    """
    Raise this to mark the success check of a task as a failure. The alternative
    is to return False from check.

    Shortcut: task.fail and task.fail_if.
    """


class TaskActionRequired(TaskSignal):
    """
    Raise this from check to pause the task and to request a manual action. The
    task status becomes ACTION_REQUIRED. It stays there until the user starts a
    check again, and check then runs one more time.

    Shortcut: task.require_action.
    """
