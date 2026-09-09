"""Interactive-only by nature: ending is a TUI concept (headless sessions
live and die with the process), and the stop machinery exists on the
interactive runner - so no tandem parametrization here."""

import logging
import threading

import pytest

from winslow.constants import Mode
from winslow.exceptions import SessionEndingError
from winslow.logger import run_logger_name
from winslow.session import SessionStatus

from harness import (
    COMPLETED_LADDER,
    STOPPED_MID_RUN,
    SWEPT,
    build_workflow,
    gated_workflow,
    run_all,
    start_gated_batch,
)


def test_quiet_end_finalizes_immediately(e2e_repo):
    """With nothing running there is no ENDING detour: end() lands directly
    on ENDED, and everything the session promised to preserve survives the
    store release."""
    workflow = build_workflow(e2e_repo, "my-workflow", Mode.TUI)
    run_all(workflow)
    session = workflow.session
    summary = session.task_status_summary

    session.end()

    assert session.status is SessionStatus.ENDED and session.has_ended
    # The snapshot is why the summary still answers after release_tasks
    # emptied the store.
    assert len(workflow.store) == 0
    assert session.task_status_summary == summary
    # History's data outlives the store release - ended sessions keep their
    # batch execution records.
    assert len(workflow.runner.record_stores()) == 1
    # The session's named logger is freed from the process-wide registry.
    assert run_logger_name(session.session_id) not in logging.Logger.manager.loggerDict
    # Idempotent - a second end must not regress ENDED or re-run finalize.
    session.end()
    assert session.status is SessionStatus.ENDED


def test_ending_waits_for_running_work(e2e_repo):
    """end() during a live batch parks at ENDING with the store intact -
    ending is patient, it never kills running work - while the admission
    gate refuses anything new. Draining alone completes the end: the runner
    calls finalize_if_drained after the last batch completion."""
    workflow, tasks = gated_workflow(e2e_repo)
    gate, batch = start_gated_batch(workflow, tasks)

    workflow.session.end()

    assert workflow.session.status is SessionStatus.ENDING
    assert not workflow.session.has_ended
    assert len(workflow.store) == 3
    with pytest.raises(SessionEndingError):
        workflow.runner.bulk_check(workflow.tasks)

    gate.set()
    batch.wait()

    assert workflow.session.status is SessionStatus.ENDED
    # The batch the end waited for finished normally, work and all.
    for name in ("Gated", "TailOne", "TailTwo"):
        workflow.store.assert_history_equals(tasks[name], COMPLETED_LADDER)
        assert workflow.target[tasks[name]] is True


def test_submitted_batch_is_visible_before_it_starts(e2e_repo):
    """The admission gate closes at submit, not at first task: a batch that
    has been submitted but whose worker hasn't reached any task yet already
    holds the session open, so end() parks at ENDING instead of finalizing
    over a batch it couldn't see."""
    workflow, tasks = gated_workflow(e2e_repo)
    gate = threading.Event()
    workflow.target[("gate",)] = gate
    batch = workflow.runner.submit_run(workflow.tasks)

    # No wait-for-RUNNING on purpose: registration alone must be enough.
    workflow.session.end()
    assert workflow.session.status is SessionStatus.ENDING

    gate.set()
    batch.wait()

    assert workflow.session.status is SessionStatus.ENDED
    for name in ("Gated", "TailOne", "TailTwo"):
        workflow.store.assert_history_equals(tasks[name], COMPLETED_LADDER)


def test_force_end_stops_the_batch(e2e_repo):
    """force_end ends the session, then sweeps stop across its batches.
    Sequential execution pins who is where: Gated is mid-run, the tails are
    queued behind it."""
    workflow, tasks = gated_workflow(e2e_repo, "--disable-concurrency")
    gate, batch = start_gated_batch(workflow, tasks)

    workflow.session.force_end()
    assert workflow.session.status is SessionStatus.ENDING

    gate.set()
    batch.wait()

    assert workflow.session.status is SessionStatus.ENDED
    # The stop was seen only after the run returned: the work landed in the
    # target, but the batch won't vouch for it - ABORTED, not COMPLETED.
    workflow.store.assert_history_equals(tasks["Gated"], STOPPED_MID_RUN)
    assert workflow.target[tasks["Gated"]] is True
    # The tails never started; the sweep is the only thing that touched them.
    for name in ("TailOne", "TailTwo"):
        workflow.store.assert_history_equals(tasks[name], SWEPT)
        assert tasks[name] not in workflow.target
