"""The MCP endpoint contract: the tools over ActionHandler behind the /mcp
mount, one bearer token for both doors, and the door switches on ServeApp.
The MCP client runs over an in-process ASGI transport with the app lifespan
entered by hand (a mounted MCP app starts through the parent lifespan)."""

import asyncio
import json

import pytest

from winslow.constants import Mode
from winslow.exceptions import MisconfigurationError
from winslow.serve import Credentials, create_app
from winslow.serve.app import ServeApp
from winslow.session import Session, SessionRegistry
from winslow.task.status import TaskStatus as S

from harness import build_workflow, by_name, wait_for_status

TOKEN = "test-token"


def registered(e2e_repo):
    workflow = build_workflow(e2e_repo, "my-workflow", Mode.TUI)
    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    registry = SessionRegistry()
    registry.register(session)
    return workflow, session, registry


def mcp_app(registry, credentials=None):
    credentials = credentials or Credentials(token=TOKEN, require_credential=True)
    return create_app(
        registry, credentials, mcp=True, base_url="http://testserver"
    )


def unwrap(outcome):
    """The tool result as plain data. The SDK renders a list return as one
    text item per element and a dict return as one item."""
    if outcome.structured_content is not None:
        return outcome.structured_content
    items = [json.loads(item.text) for item in outcome.content]
    return items[0] if len(items) == 1 else items


def call_tools(app, calls, token=TOKEN):
    """Run MCP tool calls against the app in-process; returns their results
    as plain data. The app lifespan runs around the client, the way uvicorn
    runs it."""
    import httpx2
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def main():
        results = []
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with app.router.lifespan_context(app):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(
                transport=transport, base_url="http://testserver", headers=headers
            ) as http:
                async with streamable_http_client(
                    "http://testserver/mcp", http_client=http
                ) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        for name, args in calls:
                            outcome = await session.call_tool(name, args)
                            results.append(unwrap(outcome))
        return results

    return asyncio.run(main())


def test_the_action_tools_drive_a_session_end_to_end(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    alpha = by_name(workflow)["Alpha"]

    listed, ack = call_tools(
        mcp_app(registry),
        [
            ("list_sessions", {}),
            (
                "run_tasks",
                {"session_id": session.session_id, "keys": [alpha.identity_key]},
            ),
        ],
    )
    row = listed[0] if isinstance(listed, list) else listed
    assert row["session_id"] == session.session_id
    assert ack["accepted"] is True
    assert ack["batch_uuid"]
    wait_for_status(workflow, alpha, S.COMPLETED)


def test_a_refusal_reaches_the_agent_as_data(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    (ack,) = call_tools(
        mcp_app(registry),
        [("stop_batch", {"session_id": session.session_id, "batch_uuid": "nope"})],
    )
    assert ack["accepted"] is False
    assert "nope" in ack["reason"]


def test_an_unknown_session_refuses_with_direction(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    (ack,) = call_tools(
        mcp_app(registry), [("run_tasks", {"session_id": "gone", "keys": ["k"]})]
    )
    assert ack["accepted"] is False
    assert "list_sessions shows the live ones" in ack["reason"]


def test_a_wrong_token_is_refused_before_any_tool(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    with pytest.raises(Exception):
        call_tools(mcp_app(registry), [("list_sessions", {})], token="wrong")


def test_history_and_task_detail_serve_reads(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    (ack,) = call_tools(
        mcp_app(registry),
        [("run_tasks", {"session_id": session.session_id, "keys": [alpha.identity_key]})],
    )
    wait_for_status(workflow, alpha, S.COMPLETED)
    # A fresh app: the SDK session manager runs once per process lifespan.
    history, detail = call_tools(
        mcp_app(registry),
        [
            ("history", {"session_id": session.session_id}),
            (
                "task_detail",
                {"session_id": session.session_id, "task_key": alpha.identity_key},
            ),
        ],
    )
    (batch,) = history["batches"]
    assert batch["uuid"] == ack["batch_uuid"]
    assert batch["tasks"][alpha.identity_key] == "COMPLETED"
    assert detail["key"] == alpha.identity_key


def test_a_loopback_bind_serves_mcp_without_auth(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    app = mcp_app(registry, credentials=Credentials(require_credential=False))
    (listed,) = call_tools(app, [("list_sessions", {})], token=None)
    row = listed[0] if isinstance(listed, list) else listed
    assert row["session_id"] == session.session_id


def test_the_mcp_door_without_the_token_refuses_at_build():
    with pytest.raises(MisconfigurationError, match="needs WINSLOW_TOKEN"):
        create_app(
            SessionRegistry(),
            Credentials(token=None, require_credential=True),
            mcp=True,
        )


def test_a_serve_app_needs_at_least_one_door():
    with pytest.raises(MisconfigurationError, match="at least one endpoint"):
        ServeApp(
            SessionRegistry(),
            Credentials(require_credential=False),
            ws=False,
            mcp=False,
        )


def test_the_cli_parses_the_door_flags():
    from winslow.orchestrator import Orchestrator

    args = Orchestrator.get_base_parser().parse_args(["serve", "--mcp", "--no-ws"])
    assert args.mcp is True
    assert args.no_ws is True


def test_the_tasks_tool_serves_the_keys_for_run_tasks(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    (result,) = call_tools(
        mcp_app(registry), [("tasks", {"session_id": session.session_id})]
    )
    assert result["tasks"] == {
        key: status.name for key, status in workflow.store.current.items()
    }


def test_the_descriptors_tool_matches_the_websocket_shape(e2e_repo):
    from winslow.serve.wire import descriptor_rows

    from test_serve_actions import serve_orchestrator

    orchestrator = serve_orchestrator(e2e_repo)
    app = create_app(
        SessionRegistry(),
        Credentials(token=TOKEN, require_credential=True),
        mcp=True,
        base_url="http://testserver",
        orchestrator=orchestrator,
    )
    (result,) = call_tools(app, [("descriptors", {})])
    assert result == descriptor_rows(orchestrator)
    assert {"workflows", "overrides"} <= set(result)
