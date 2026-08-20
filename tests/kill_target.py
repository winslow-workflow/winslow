"""The victim of the SIGKILL restore test: a persisted TUI-mode session of
my-gates whose batch stays open on the gate. It prints its session id when
the batch is verifiably busy, then hangs until the test kills the process."""

import sys
import threading
import time

from winslow.constants import Mode
from winslow.orchestrator import Orchestrator, OrchestratorConfig
from winslow.session import Session
from winslow.state import create_state_store
from winslow.task.status import PASSING_STATUSES, TaskStatus


def main(directory):
    parser = Orchestrator.get_base_parser()
    config = parser.parse_args(
        ["run", "--mode", Mode.TUI.value, "--workflow", "my-gates"],
        namespace=OrchestratorConfig(),
    )
    orchestrator = Orchestrator(config, directory=directory)
    orchestrator.workflow_registry.collect_classes(directory)
    workflow = orchestrator.workflow_registry["my-gates"](config)
    workflow.initialize_tasks()

    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    workflow.init_state(
        create_state_store(config),
        origin="tui",
        orchestrator_overrides={},
        workflow_values={},
    )

    # The gate never sets, so the batch stays open until the SIGKILL.
    workflow.target[("gate",)] = threading.Event()
    tasks = {type(task).__name__: task for task in workflow.tasks}
    batch = workflow.runner.submit_run(workflow.tasks)

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        settled = all(
            workflow.store[tasks[name]] in PASSING_STATUSES
            for name in ("TailOne", "TailTwo")
        )
        if settled and workflow.store[tasks["Gated"]] is TaskStatus.RUNNING:
            break
        time.sleep(0.01)
    else:
        raise SystemExit("the batch never reached the busy state")

    # The snapshot writes are queued; the settled tails must be on disk before
    # the parent reads the printed id and kills this process.
    workflow.persistence_listener.flush()
    print(session.session_id, flush=True)
    batch.wait()


if __name__ == "__main__":
    main(sys.argv[1])
