"""The SIGKILL restore test: a real process dies mid-batch, and a fresh
process seeds the session from what landed on disk."""

import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

from winslow.constants import Mode
from winslow.orchestrator import OrchestratorConfig
from winslow.runner.execution import ExecutionStatus
from winslow.session import Session
from winslow.state import FileStateStore
from winslow.task.status import TaskStatus as S

from harness import build_workflow, by_name

KILL_TARGET = Path(__file__).parent / "kill_target.py"


def _read_session_id(stream, timeout=60.0):
    """A bounded readline: a victim that never prints fails the test instead
    of hanging it."""
    ready, _, _ = select.select([stream], [], [], timeout)
    if not ready:
        return ""
    return stream.readline().strip()


@pytest.mark.slow
def test_a_killed_session_restores_in_a_fresh_process(e2e_repo, tmp_path):
    state_dir = tmp_path / "state"
    env = {
        **os.environ,
        "WINSLOW_STATE_DIR": str(state_dir),
        "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),
    }
    # stderr goes to a file: an undrained pipe deadlocks the victim once it
    # fills, and the file still carries the diagnostics on a failure.
    stderr_path = tmp_path / "victim-stderr.log"
    with stderr_path.open("w") as stderr:
        victim = subprocess.Popen(
            [sys.executable, str(KILL_TARGET), str(e2e_repo)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
        )
    try:
        session_id = _read_session_id(victim.stdout)
        assert session_id, stderr_path.read_text()
    finally:
        victim.kill()  # SIGKILL: no cleanup code of the victim runs
        victim.wait(timeout=10)

    class TmpStateStore(FileStateStore):
        base_directory = state_dir

    store = TmpStateStore(OrchestratorConfig())
    workflow = build_workflow(e2e_repo, "my-gates", Mode.TUI)
    Session(workflow, session_id=session_id)
    workflow.check_pipeline_eligibility()
    workflow.init_state(store, origin="tui")
    workflow.seed_from_state()

    tasks = by_name(workflow)
    # The tails settled before the death; with no TTL their successes seed as
    # STALE. The gated task died mid-run; its last terminal entry is the
    # failed pre-run check.
    assert workflow.store[tasks["TailOne"]] is S.STALE
    assert workflow.store[tasks["TailTwo"]] is S.STALE
    assert workflow.store[tasks["Gated"]] is S.FAILED

    interrupted = [
        batch
        for batch in workflow.runner.batches
        if batch.status is ExecutionStatus.INTERRUPTED
    ]
    assert len(interrupted) == 1
    assert store.load_open_batches(session_id) == []
    # The manifest survived the death and stayed open until this restore.
    (manifest,) = store.list_open_manifests()
    assert manifest.session_id == session_id
