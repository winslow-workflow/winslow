"""The MCP endpoint: the tool layer over ActionHandler (serve-spec section
5). One tool per action; the acks travel as tool results, so an agent reads
the refusal reason as data. Blocking work runs on worker threads. Requires
the [mcp] extra."""

import asyncio
import hmac
from dataclasses import asdict

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from winslow.actions import (
    CheckTasks,
    EndSession,
    RunTasks,
    SetBatchOptions,
    StopBatch,
)
from winslow.exceptions import MisconfigurationError
from winslow.serve.sessions import create_session
from winslow.serve.wire import descriptor_rows, history_rows, session_row
from winslow.task.info import TaskInfo


class BearerTokenVerifier(TokenVerifier):
    """The plain bearer token of the serve credentials: the same
    WINSLOW_TOKEN the websocket hello accepts."""

    def __init__(self, token):
        self._token = token

    async def verify_token(self, token):
        if hmac.compare_digest(token, self._token):
            return AccessToken(token=token, client_id="winslow-token", scopes=[])
        return None


def tool(method):
    """Mark a method as an MCP tool. _register_tools collects the marked
    methods in declaration order (see McpEndpoint)."""
    method._mcp_tool = True
    return method


class McpEndpoint:
    """The MCPServer of one serve process. The tools resolve sessions on the
    registry of the ServeApp and submit through submit_guarded, so a broken
    action reaches the agent as a refused ack, never as a raise."""

    def __init__(self, serve_app, base_url):
        self.serve_app = serve_app
        self._base_url = base_url
        self.server = MCPServer(
            name="winslow",
            instructions=(
                "The live winslow sessions of this server. list_sessions and "
                "descriptors show what runs and what can run; the action tools "
                "answer with an ack: accepted, or refused with the reason."
            ),
            **self._auth_settings(base_url),
        )
        self._register_tools()

    def _auth_settings(self, base_url):
        credentials = self.serve_app.credentials
        if not credentials.require_credential:
            return {}
        if not credentials.token:
            raise MisconfigurationError(
                "The MCP endpoint needs WINSLOW_TOKEN on a non-loopback bind - "
                "set it, or bind to loopback."
            )
        # The two URLs are required OAuth fields; the serve base URL fills
        # both, and the auto-served resource metadata stays inert until real
        # OAuth arrives (see serve-spikes-findings, spike 3).
        return {
            "token_verifier": BearerTokenVerifier(credentials.token),
            "auth": AuthSettings(
                issuer_url=AnyHttpUrl(base_url),
                resource_server_url=AnyHttpUrl(f"{base_url}/mcp"),
            ),
        }

    def streamable_http_app(self):
        # The SDK's DNS-rebinding protection allows loopback hosts only by
        # default; the serve host must pass its own Host header.
        host = self._base_url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        return self.server.streamable_http_app(
            transport_security=TransportSecuritySettings(
                allowed_hosts=[host, f"{host}:*", "127.0.0.1:*", "localhost:*"]
            )
        )

    @property
    def session_manager(self):
        return self.server.session_manager

    async def _submit(self, session_id, action):
        """The ack of the action as a dict, or the refusal shape for a
        session id that resolves nothing."""
        session = self.serve_app.registry.get(session_id)
        if session is None:
            return {
                "accepted": False,
                "reason": f"session id {session_id!r} does not resolve to a "
                f"live session - list_sessions shows the live ones.",
            }
        ack = await asyncio.to_thread(session.actions.submit_guarded, action)
        return asdict(ack)

    def _register_tools(self):
        for name, member in type(self).__dict__.items():
            if getattr(member, "_mcp_tool", False):
                self.server.add_tool(getattr(self, name))

    @tool
    async def list_sessions(self) -> list:
        """The live sessions of this server."""
        return [session_row(s) for s in self.serve_app.registry.sessions()]

    @tool
    async def run_tasks(self, session_id: str, keys: list[str]) -> dict:
        """Submit a run batch for the given task identity keys."""
        return await self._submit(session_id, RunTasks(keys=tuple(keys)))

    @tool
    async def check_tasks(self, session_id: str, keys: list[str]) -> dict:
        """Submit a check batch for the given task identity keys."""
        return await self._submit(session_id, CheckTasks(keys=tuple(keys)))

    @tool
    async def stop_batch(self, session_id: str, batch_uuid: str) -> dict:
        """Request a stop of one live batch."""
        return await self._submit(session_id, StopBatch(batch_uuid=batch_uuid))

    @tool
    async def end_session(self, session_id: str, force: bool = False) -> dict:
        """End the session; force stops its running batches first."""
        return await self._submit(session_id, EndSession(force=force))

    @tool
    async def set_batch_options(
        self,
        session_id: str,
        dry_run: bool | None = None,
        force_run: bool | None = None,
        force_success: bool | None = None,
        disable_concurrency: bool | None = None,
    ) -> dict:
        """Set the batch options of the session; a None field stays
        unchanged."""
        return await self._submit(
            session_id,
            SetBatchOptions(
                dry_run=dry_run,
                force_run=force_run,
                force_success=force_success,
                disable_concurrency=disable_concurrency,
            ),
        )

    @tool
    async def descriptors(self) -> list:
        """The workflows this server can start, with their options."""
        if self.serve_app.orchestrator is None:
            return [{"error": "this server serves no workflows"}]
        return descriptor_rows(self.serve_app.orchestrator)

    @tool
    async def start_session(
        self, workflow: str, overrides: dict | None = None, values: dict | None = None
    ) -> dict:
        """Create one live session of the named workflow."""
        serve_app = self.serve_app
        if serve_app.orchestrator is None or serve_app.state_store is None:
            return {"error": "this server creates no sessions"}
        try:
            session = await asyncio.to_thread(
                create_session,
                serve_app.orchestrator,
                serve_app.state_store,
                serve_app.registry,
                workflow,
                overrides,
                values,
            )
        except Exception as exc:
            return {"error": str(exc.args[0] if exc.args else exc)}
        return session_row(session)

    @tool
    async def tasks(self, session_id: str) -> dict:
        """The tasks of the session: {identity key: status}. The keys feed
        run_tasks and check_tasks."""
        session = self.serve_app.registry.get(session_id)
        if session is None:
            return {"error": f"session id {session_id!r} does not resolve"}
        store = session.workflow.store
        return {"tasks": {key: status.name for key, status in store.current.items()}}

    @tool
    async def history(self, session_id: str) -> dict:
        """The batches of the session with their per-task outcomes."""
        session = self.serve_app.registry.get(session_id)
        if session is None:
            return {"error": f"session id {session_id!r} does not resolve"}
        return {"batches": history_rows(session)}

    @tool
    async def task_detail(self, session_id: str, task_key: str) -> dict:
        """The full capture of one task: attributes, docs, source."""
        session = self.serve_app.registry.get(session_id)
        if session is None:
            return {"error": f"session id {session_id!r} does not resolve"}
        try:
            task = session.workflow.task_index.resolve(task_key)
        except KeyError as exc:
            return {"error": exc.args[0]}
        info = await asyncio.to_thread(TaskInfo.from_task, task, full=True)
        return asdict(info)
