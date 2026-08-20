from enum import Enum, auto


class TaskStatus(Enum):
    INITIALIZED = auto()

    CHECKING_ELIGIBILITY = auto()  # is_eligible check
    SKIPPED = auto()  # is_eligible check failed

    READY_TO_PROCESS = auto()  # the task can run or check its completion
    READY_TO_RUN = auto()  # can_run check succeeded

    CHECKING_RUNNABILITY = auto()
    RUNNING = auto()

    # The run of the task is finished. This does not mean that the task is
    # successful. The completion check decides that.
    RUN_FINISHED = auto()

    FAILED = auto()  # check() returned False
    ERROR = auto()

    # can_run or can_check failed, or a dependency has a failing
    # status.
    BLOCKED = auto()

    CHECKING_DEPENDENCIES = auto()
    CHECKING_COMPLETION = auto()  # runs check()

    ACTION_REQUIRED = auto()  # manual action prompt

    COMPLETED = auto()  # the task is successful

    # The check passed, but the task errored after its last run attempt. For
    # example, run() did its work and then raised. This is different from
    # COMPLETED, so a check that passes cannot hide a defect. Only a clean new run
    # clears it.
    COMPLETED_WITH_ERROR = auto()

    # Marked successful by the force_success run option, with no check (see
    # runner.process_task).
    FORCE_SUCCESS = auto()

    # The check passed although this workflow never ran the task. The work
    # exists from outside the workflow, for example from an earlier session
    # or from another system. A noop task never gets this status: it has no
    # run action, so "previously" does not apply (see
    # BaseRunner._check_task_success).
    COMPLETED_PREVIOUSLY = auto()

    # A passing status whose snapshot is no longer trusted (see is_trusted).
    # The next touch re-verifies it. Never persisted: the snapshot keeps the
    # real outcome, and a restore re-derives the trust.
    STALE = auto()

    ABORTED = auto()  # the batch stopped before the task completed

    # For a batch record only, and never written to the main store. The batch
    # waits on the claim of another batch on this task (see
    # InteractiveRunner._claim_task).
    WAITING_FOR_RELEASE = auto()

    def __str__(self):
        return self.name.replace("_", " ")


# A task with one of these statuses is in progress and resolves without help. A
# downstream process waits for it.
ACTIVE_STATUSES = frozenset(
    (
        TaskStatus.CHECKING_ELIGIBILITY,
        TaskStatus.CHECKING_RUNNABILITY,
        TaskStatus.RUNNING,
        TaskStatus.CHECKING_DEPENDENCIES,
        TaskStatus.CHECKING_COMPLETION,
    )
)


# READY_TO_RUN is not in this set. It is an intermediate state that the runner
# sets.

RUNNABLE_STATUSES = frozenset(
    (
        TaskStatus.INITIALIZED,
        TaskStatus.READY_TO_PROCESS,
        TaskStatus.FAILED,
        TaskStatus.ERROR,
        TaskStatus.BLOCKED,
        TaskStatus.ABORTED,
        TaskStatus.RUN_FINISHED,
        TaskStatus.STALE,
    )
)

CHECKABLE_STATUSES = RUNNABLE_STATUSES.union(
    [
        TaskStatus.ACTION_REQUIRED,
        TaskStatus.COMPLETED,
        TaskStatus.COMPLETED_WITH_ERROR,
        TaskStatus.COMPLETED_PREVIOUSLY,
        TaskStatus.FORCE_SUCCESS,
    ]
)


# Statuses that mean the task is done and successful. It passes and it satisfies
# each task that depends on it.
PASSING_STATUSES = frozenset(
    (
        TaskStatus.COMPLETED,
        TaskStatus.COMPLETED_WITH_ERROR,
        TaskStatus.COMPLETED_PREVIOUSLY,
        TaskStatus.FORCE_SUCCESS,
    )
)


# Statuses that leave a dependency unsatisfied: each status except a passing one
# and a skip. A skipped dependency counts as satisfied.
UNSATISFIED_STATUSES = frozenset(TaskStatus).difference(
    PASSING_STATUSES, {TaskStatus.SKIPPED}
)


# Statuses that mean the task is in a bad state and needs attention. STALE
# counts: the success is no longer trusted until a re-verification.
PROBLEMATIC_STATUSES = frozenset(
    (
        TaskStatus.FAILED,
        TaskStatus.ERROR,
        TaskStatus.BLOCKED,
        TaskStatus.ABORTED,
        TaskStatus.STALE,
    )
)


# Statuses that mean a run did not reach its expected outcome: the problematic
# ends and ACTION_REQUIRED, which waits for a person. The headless exit code uses
# this set.
UNSUCCESSFUL_STATUSES = PROBLEMATIC_STATUSES.union((TaskStatus.ACTION_REQUIRED,))


# Statuses that persist as task snapshots: the terminal outcomes that a
# restore can seed. Every other status is transient (see winslow.state).
SNAPSHOT_STATUSES = PASSING_STATUSES.union((TaskStatus.FAILED, TaskStatus.ERROR))
