"""The shutdown drain of a serve process: the running batches stop at the next
task boundary, and the wait has a bound (see ServeApp.drain)."""

import asyncio
import threading
import time

import anyio
import pytest

from winslow.client.base import SessionClient
from winslow.constants import Mode
from winslow.runner.execution import ExecutionStatus
from winslow.serve.app import ServeApp
from winslow.serve.server import HttpServer
from winslow.session import Session, SessionRegistry

from harness import serve_authenticator
from harness import bare_orchestrator, build_workflow, by_name, scratch_state_store


def served_session(e2e_repo):
    workflow = build_workflow(e2e_repo, "my-workflow", Mode.TUI)
    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    registry = SessionRegistry()
    registry.register(session)
    app = ServeApp(
        registry,
        serve_authenticator(),
        orchestrator=bare_orchestrator(),
        state_store=scratch_state_store(),
    )
    return workflow, session, app


def running_batch(workflow, session, keys):
    ack = session.actions.submit_guarded(
        SessionClient.run_tasks(keys=keys, options={"disable_concurrency": True})
    )
    assert ack.accepted, ack.reason
    batch = workflow.runner.get_batch(ack.batch_uuid)
    deadline = time.monotonic() + 5
    while batch.status is not ExecutionStatus.RUNNING and time.monotonic() < deadline:
        time.sleep(0.01)
    return batch


def test_drain_stops_the_chain_at_the_next_task_boundary(e2e_repo, monkeypatch):
    workflow, session, app = served_session(e2e_repo)
    tasks = by_name(workflow)
    chain = [tasks["Alpha"], tasks["Beta"], tasks["Gamma"]]

    def slow_run(self):
        time.sleep(0.3)
        self.target[self] = True

    for task in chain:
        monkeypatch.setattr(type(task), "run", slow_run)
    batch = running_batch(workflow, session, tuple(t.identity_key for t in chain))

    started = time.monotonic()
    stuck = app.drain(5.0)
    elapsed = time.monotonic() - started

    assert stuck == []
    assert batch.status is ExecutionStatus.STOPPED
    assert elapsed < 2.0
    # The first task ran to its end, the rest of the chain never started.
    assert tasks["Alpha"] in workflow.workflow_config.target
    assert tasks["Gamma"] not in workflow.workflow_config.target


def test_drain_returns_a_batch_stuck_inside_a_task_after_the_bound(
    e2e_repo, monkeypatch
):
    workflow, session, app = served_session(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    release = threading.Event()

    def blocking_run(self):
        release.wait(timeout=10)
        self.target[self] = True

    monkeypatch.setattr(type(alpha), "run", blocking_run)
    batch = running_batch(workflow, session, (alpha.identity_key,))

    started = time.monotonic()
    stuck = app.drain(0.2)
    elapsed = time.monotonic() - started

    assert stuck == [batch]
    assert 0.2 <= elapsed < 1.0
    release.set()
    assert batch.join(5.0)


def mcp_session(e2e_repo):
    workflow = build_workflow(e2e_repo, "my-workflow", Mode.TUI)
    session = Session(workflow)
    registry = SessionRegistry()
    registry.register(session)
    return ServeApp(
        registry,
        serve_authenticator(),
        orchestrator=bare_orchestrator(),
        state_store=scratch_state_store(),
        endpoints=("ws", "mcp"),
    )


def open_listen_stream(app):
    """A listen stream the way ListenHandler holds one: the send half of a
    buffered memory stream, registered on the handler."""
    entry = app.mcp_endpoint.server._lowlevel_server.get_request_handler(
        "subscriptions/listen"
    )
    send, recv = anyio.create_memory_object_stream(8)
    entry.handler._streams.add(send)
    return send, recv


def test_close_streams_ends_every_listen_stream(e2e_repo):
    app = mcp_session(e2e_repo)
    first, first_recv = open_listen_stream(app)
    second, second_recv = open_listen_stream(app)

    app.close_streams()

    for send in (first, second):
        with pytest.raises(anyio.ClosedResourceError):
            send.send_nowait("event")
    for recv in (first_recv, second_recv):
        with pytest.raises(anyio.EndOfStream):
            recv.receive_nowait()


def test_close_streams_without_the_mcp_endpoint(e2e_repo):
    _, _, app = served_session(e2e_repo)
    assert app.mcp_endpoint is None
    app.close_streams()


def test_http_server_shutdown_ends_the_streams_before_the_connection_wait(e2e_repo):
    app = mcp_session(e2e_repo)
    send, _ = open_listen_stream(app)
    server = HttpServer(app, host="127.0.0.1", port=0, log_config=None)
    # A server that never started has no listeners and no lifespan to stop.
    server.servers = []
    server.force_exit = True

    asyncio.run(server.shutdown())

    with pytest.raises(anyio.ClosedResourceError):
        send.send_nowait("event")
