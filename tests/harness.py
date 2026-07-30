import hashlib
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from winslow.constants import Mode
from winslow.orchestrator import Orchestrator, OrchestratorConfig
from winslow.session import Session
from winslow.task.status import TaskStatus as S
from winslow.util import _SCOPE_NS

E2E_REPO = Path(__file__).parent / "e2e_repo"


# The eligibility pre-pass every task gets before any batch touches it - the
# shared prefix of every ladder below (except SKIPPED, decided mid-pass).
UNPROCESSED = [S.INITIALIZED, S.CHECKING_ELIGIBILITY, S.READY_TO_PROCESS]

# A fresh (empty-target) task always fails its up-front completion check and
# proceeds through the run path.
PRE_RUN = UNPROCESSED + [
    S.CHECKING_COMPLETION,
    S.FAILED,
    S.CHECKING_DEPENDENCIES,
    S.CHECKING_RUNNABILITY,
]
RUN_AND_CHECK = [S.READY_TO_RUN, S.RUNNING, S.RUN_FINISHED, S.CHECKING_COMPLETION]

COMPLETED_LADDER = PRE_RUN + RUN_AND_CHECK + [S.COMPLETED]
FAILED_LADDER = PRE_RUN + RUN_AND_CHECK + [S.FAILED]
# The runnability gate refuses and the ladder aborts. A single BLOCKED entry:
# the abort path relabels the status, but the store drops redundant writes.
BLOCKED_LADDER = PRE_RUN + [S.BLOCKED]
# Eligibility is decided before any run/check machinery - the shortest ladder.
SKIPPED_LADDER = [S.INITIALIZED, S.CHECKING_ELIGIBILITY, S.SKIPPED]

# A passing pure check. Also what a pre-seeded run collapses into: the
# up-front completion check passes, so the run path (dependencies /
# runnability / run) is never entered. The workflow never ran the task,
# so the passing check lands COMPLETED_PREVIOUSLY, not COMPLETED.
CHECK_PASSED_LADDER = UNPROCESSED + [S.CHECKING_COMPLETION, S.COMPLETED_PREVIOUSLY]

# A failing pure check: checks observe, they never repair - there is no run
# step to fall through to.
CHECK_FAILED_LADDER = UNPROCESSED + [S.CHECKING_COMPLETION, S.FAILED]

# force_success short-circuits before the check phase - CHECKING_COMPLETION
# never appears, because the point of the flag is to not consult the checks.
FORCE_SUCCESS_LADDER = UNPROCESSED + [S.FORCE_SUCCESS]

# A task that fails its up-front check, then blocks on a failed dependency
# before ever reaching its own runnability gate.
DEP_BLOCKED_LADDER = UNPROCESSED + [
    S.CHECKING_COMPLETION,
    S.FAILED,
    S.CHECKING_DEPENDENCIES,
    S.BLOCKED,
]

# A settled task's next run batch: no eligibility re-pass (the TUI runs it
# once at init), so the second ascent starts at the completion check and
# climbs the full run ladder from there.
RERUN_LADDER = (
    [
        S.CHECKING_COMPLETION,
        S.FAILED,
        S.CHECKING_DEPENDENCIES,
        S.CHECKING_RUNNABILITY,
    ]
    + RUN_AND_CHECK
    + [S.COMPLETED]
)

# The in-flight task at a stop: stop is cooperative, so the run completes
# (and its work lands) - but the post-run check is skipped, because the batch
# will not vouch for work it was told to abandon.
STOPPED_MID_RUN = PRE_RUN + [S.READY_TO_RUN, S.RUNNING, S.RUN_FINISHED, S.ABORTED]

# Swept before ever starting: the eligibility pre-pass, then the abort.
SWEPT = UNPROCESSED + [S.ABORTED]


@contextmanager
def workflow_repo(directory):
    """Import scope for a workflow repo, torn down afterwards: the scoped
    modules (and any repo-root shared modules imported by bare name) leave
    sys.modules and the repo leaves sys.path, so repos can't contaminate
    each other across sessions."""
    directory = str(Path(directory).resolve())
    scope_prefix = _SCOPE_NS + hashlib.md5(directory.encode()).hexdigest()[:8]
    try:
        yield directory
    finally:
        for name, module in list(sys.modules.items()):
            file = getattr(module, "__file__", None) or ""
            if name.startswith(scope_prefix) or file.startswith(directory + os.sep):
                del sys.modules[name]
        if directory in sys.path:
            sys.path.remove(directory)


def build_workflow(directory, name, mode, *extra_argv):
    """A workflow initialized the way the real entrypoints do it; extra_argv
    passes real CLI flags (e.g. "--check", "--force-success")."""
    parser = Orchestrator.get_base_parser()
    config = parser.parse_args(
        ["run", "--mode", mode.value, "--workflow", name, *extra_argv],
        namespace=OrchestratorConfig(),
    )
    orchestrator = Orchestrator(config, directory=directory)
    orchestrator.workflow_registry.collect_classes(directory)
    workflow = orchestrator.workflow_registry[name](config)
    workflow.initialize_tasks()
    return workflow


def ready(workflow):
    """Session + the eligibility pre-pass: the state a live workflow is in
    before its first batch (the UI does both at init)."""
    Session(workflow)
    workflow.check_pipeline_eligibility()
    return workflow


def run_all(workflow):
    """Full run, dispatched the way each mode really drives it: headless_run
    for the CLI path; eligibility + bulk_run for the interactive path (what
    the UI's run-all button calls, minus the UI)."""
    if workflow.orchestrator_config.mode is Mode.HEADLESS:
        workflow.headless_run()
    else:
        ready(workflow)
        workflow.runner.bulk_run(workflow.runner.eligible_tasks(workflow.tasks))


def check_all(workflow):
    """Check-only pass, dispatched the way each mode really drives it. The
    headless leg routes through headless_run, so the workflow must be built
    with "--check" - asserted, because without it the same call would RUN."""
    if workflow.orchestrator_config.mode is Mode.HEADLESS:
        assert workflow.check, "build the workflow with --check for check_all"
        workflow.headless_run()
    else:
        ready(workflow)
        workflow.runner.bulk_check(workflow.runner.eligible_tasks(workflow.tasks))


def check_batch(workflow, tasks=None):
    """A follow-up check batch, runner-level: what a re-invoked --check CLI or
    the UI's check action does. No eligibility re-pass - re-running it would
    reset every verdict, which no real second interaction does. `tasks`
    narrows the roster (what per-task UI actions do); default is the whole
    store."""
    tasks = workflow.tasks if tasks is None else tasks
    if workflow.orchestrator_config.mode is Mode.HEADLESS:
        workflow.runner.check(tasks)
    else:
        workflow.runner.bulk_check(tasks)


def run_batch(workflow, tasks=None):
    """A follow-up run batch, runner-level - same contract as check_batch."""
    tasks = workflow.tasks if tasks is None else tasks
    if workflow.orchestrator_config.mode is Mode.HEADLESS:
        workflow.runner.run(tasks)
    else:
        workflow.runner.bulk_run(tasks)


def wait_for_status(workflow, task, status, timeout=5.0):
    """Poll until the task's live status equals `status` - the bridge between
    a background batch thread and deterministic assertions."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if workflow.store[task] is status:
            return
        time.sleep(0.005)
    raise AssertionError(
        f"{task} never reached {status} (stuck at {workflow.store[task]})"
    )


def gated_workflow(e2e_repo, *extra_argv):
    workflow = ready(build_workflow(e2e_repo, "my-gates", Mode.TUI, *extra_argv))
    return workflow, by_name(workflow)


def start_gated_batch(workflow, tasks):
    """A submitted run batch, held open by the gate event; returns once the
    gated task is verifiably RUNNING, so every assertion that follows
    happens against a deterministically busy session."""
    gate = threading.Event()
    workflow.target[("gate",)] = gate
    batch = workflow.runner.submit_run(workflow.tasks)
    wait_for_status(workflow, tasks["Gated"], S.RUNNING)
    return gate, batch


def by_name(workflow):
    """{class name: task} - only valid for workflows without parameterized
    tasks (one instance per class)."""
    return {type(task).__name__: task for task in workflow.tasks}


def by_params(workflow):
    """{(class name, *parameter values): task} - parameterized classes expand
    into many instances of the same class, so bare class names would clash.
    Values are ordered by parameter name; unparameterized tasks key as
    (class name,)."""
    return {
        (
            type(task).__name__,
            *(value for _, value in sorted(task._parameters_dict.items())),
        ): task
        for task in workflow.tasks
    }
