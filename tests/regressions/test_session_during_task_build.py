"""These tests read a session while its tasks build. create_session
registered the session before initialize_tasks. A roster read in that window
iterated workflow.tasks while it was None and failed with a TypeError."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from winslow.client import LocalAppClient
from winslow.session import SessionRegistry
from winslow.workflow.workflow import Workflow

from harness import started
from test_client_local import local_orchestrator


def hold_task_build(monkeypatch):
    """Hold each initialize_tasks call until the test sets release. The held
    call sets building first, so the test knows that a build waits."""
    building = threading.Event()
    release = threading.Event()
    initialize_tasks = Workflow.initialize_tasks

    def held(workflow, **kwargs):
        building.set()
        release.wait(timeout=5)
        return initialize_tasks(workflow, **kwargs)

    monkeypatch.setattr(Workflow, "initialize_tasks", held)
    return building, release


def local_app(e2e_repo, state_store):
    registry = SessionRegistry()
    app = LocalAppClient(
        registry, orchestrator=local_orchestrator(e2e_repo), state_store=state_store
    )
    return app, registry


def test_a_session_lists_once_its_tasks_are_built(e2e_repo, state_store, monkeypatch):
    app, _ = local_app(e2e_repo, state_store)
    building, release = hold_task_build(monkeypatch)

    with ThreadPoolExecutor(max_workers=1) as pool:
        start = pool.submit(app.create_session, "my-workflow")
        try:
            assert building.wait(timeout=5)
            assert app.sessions() == ()
        finally:
            release.set()
        row = started(start.result(timeout=5))

    assert [info.session_id for info in app.sessions()] == [row.session_id]
    assert app.session(row.session_id).roster()


def test_a_restore_during_the_task_build_holds_its_id(
    e2e_repo, state_store, monkeypatch
):
    app, registry = local_app(e2e_repo, state_store)
    session_id = started(app.create_session("my-workflow")).session_id
    # A dead process: the manifest stays open, and no live session holds it.
    registry.remove(session_id)
    building, release = hold_task_build(monkeypatch)

    with ThreadPoolExecutor(max_workers=1) as pool:
        restore = pool.submit(app.restore_session, session_id)
        try:
            assert building.wait(timeout=5)
            assert session_id not in {m.session_id for m in app.manifests()}
            refused = app.restore_session(session_id)
            assert "already restoring" in refused.reason
            with pytest.raises(KeyError, match="still building its tasks"):
                app.session(session_id)
        finally:
            release.set()
        started(restore.result(timeout=5))

    assert app.session(session_id).roster()
