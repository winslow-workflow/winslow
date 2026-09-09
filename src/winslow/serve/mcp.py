"""The MCP endpoint: one tool per port read and action, over the same
LocalAppClient the local TUI consumes (see winslow.client). The acks and
the refusals travel as tool results, so an agent reads every reason as
data. Blocking work runs on worker threads. Requires the [mcp] extra."""

import hmac

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from winslow._meta import _tagged
from winslow.actions import (
    Action,
    CheckTasks,
    ClearCacheEntries,
    EndSession,
    LoadCacheEntries,
    RunTasks,
    StopBatch,
)
from winslow.client.base import AppClient, SessionClient
from winslow.exceptions import MisconfigurationError, RequestError
from winslow.protocol.codec import CODEC


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
    """The MCPServer of one serve process. The tools read through the port
    of the ServeApp and submit through submit_guarded, so a broken action
    reaches the agent as a refused ack."""

    def __init__(self, serve_app, base_url):
        self.serve_app = serve_app
        self._base_url = base_url
        self.server = MCPServer(
            name="winslow",
            instructions=(
                "The live winslow sessions of this server. list_sessions and "
                "descriptors show what runs and what can run; the read tools "
                "serve snapshots, history, logs and caches; the action tools "
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
        # AuthSettings requires both URLs. The serve base URL fills both, and the
        # resource metadata the SDK serves stays unused until OAuth exists.
        return {
            "token_verifier": BearerTokenVerifier(credentials.token),
            "auth": AuthSettings(
                issuer_url=AnyHttpUrl(base_url),
                resource_server_url=AnyHttpUrl(f"{base_url}/mcp"),
            ),
        }

    def streamable_http_app(self):
        # The DNS rebinding protection of the SDK allows loopback hosts only by
        # default. The serve host must pass its own Host header.
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
        """The ack of the action as a dict (see ServeApp.submit). An unknown
        session id answers the refused ack shape, so an agent reads it as data."""
        try:
            ack = await self.serve_app.submit(session_id, action)
        except RequestError as exc:
            return {"accepted": False, "reason": str(exc)}
        return CODEC.dump(ack)

    async def _read(self, spec, *args, session_id=None, **kwargs):
        """One port read as a tool result (see ServeApp.read). A refusal
        answers {'error': reason}, so an agent reads it as data."""
        try:
            result = await self.serve_app.read(
                spec, session_id, spec.bind(args, kwargs)
            )
        except RequestError as exc:
            return {"error": str(exc)}
        return CODEC.dump(result)

    def _register_tools(self):
        for method in _tagged(type(self), "_mcp_tool"):
            self.server.add_tool(getattr(self, method.__name__))

    @tool
    async def list_sessions(self) -> list:
        """The live sessions of this server."""
        return await self._read(AppClient.sessions)

    @tool
    async def run_tasks(
        self, session_id: str, keys: list[str], options: dict | None = None
    ) -> dict:
        """Submit a run batch for the given task identity keys. options
        carries the batch options of this submit, for example
        {"force_run": true} (see batch_options for the baseline)."""
        return await self._submit(
            session_id,
            Action.build(RunTasks.name, dict(keys=keys, options=options)),
        )

    @tool
    async def check_tasks(
        self, session_id: str, keys: list[str], options: dict | None = None
    ) -> dict:
        """Submit a check batch for the given task identity keys. options
        works as in run_tasks."""
        return await self._submit(
            session_id,
            Action.build(CheckTasks.name, dict(keys=keys, options=options)),
        )

    @tool
    async def stop_batch(self, session_id: str, batch_uuid: str) -> dict:
        """Request a stop of one live batch."""
        return await self._submit(
            session_id, Action.build(StopBatch.name, dict(batch_uuid=batch_uuid))
        )

    @tool
    async def end_session(self, session_id: str, force: bool = False) -> dict:
        """End the session. force stops its running batches first."""
        return await self._submit(
            session_id, Action.build(EndSession.name, dict(force=force))
        )

    @tool
    async def load_cache_entries(self, session_id: str, entries: list) -> dict:
        """Load the given cache entries. Each entry is a
        [cache_name, entry_name] pair (see caches)."""
        return await self._submit(
            session_id, Action.build(LoadCacheEntries.name, dict(entries=entries))
        )

    @tool
    async def clear_cache_entries(self, session_id: str, entries: list) -> dict:
        """Clear the given cache entries. entries works as in
        load_cache_entries."""
        return await self._submit(
            session_id, Action.build(ClearCacheEntries.name, dict(entries=entries))
        )

    @tool
    async def descriptors(self) -> dict:
        """The workflows this server can start (their options fill the
        `values` of start_session) and the orchestrator overrides."""
        return await self._read(AppClient.descriptors)

    @tool
    async def manifests(self) -> list:
        """The open manifests of dead sessions. restore_session rebuilds
        one under its stored session id."""
        return await self._read(AppClient.manifests)

    @tool
    async def start_session(
        self, workflow: str, overrides: dict | None = None, values: dict | None = None
    ) -> dict:
        """Create one live session of the named workflow."""
        return await self._read(AppClient.create_session, workflow, overrides, values)

    @tool
    async def restore_session(self, session_id: str) -> dict:
        """Rebuild one dead session from its open manifest (see manifests)."""
        return await self._read(AppClient.restore_session, session_id)

    @tool
    async def snapshot(self, session_id: str) -> dict:
        """The current state of the session: `tasks` maps each identity key
        to its status (the keys feed run_tasks and check_tasks), plus the
        batches and the session log backlog."""
        return await self._read(SessionClient.snapshot, session_id=session_id)

    @tool
    async def roster(self, session_id: str) -> list:
        """One stub task capture per task, in launch order."""
        return await self._read(SessionClient.roster, session_id=session_id)

    @tool
    async def task_detail(self, session_id: str, task_key: str) -> dict:
        """The full capture of one task: attributes, docs, source."""
        return await self._read(
            SessionClient.task_detail, task_key, session_id=session_id
        )

    @tool
    async def history(self, session_id: str) -> list:
        """The batches of the session with their per-task outcomes."""
        return await self._read(SessionClient.history, session_id=session_id)

    @tool
    async def record_detail(
        self, session_id: str, batch_uuid: str, task_key: str
    ) -> dict:
        """The execution record of one task in one batch: the task capture
        after the run, its phases, and its snapshots."""
        return await self._read(
            SessionClient.record_detail, batch_uuid, task_key, session_id=session_id
        )

    @tool
    async def log_tail(
        self, session_id: str, batch_uuid: str, task_key: str, limit: int | None = None
    ) -> list:
        """The stored log lines of one task in one batch."""
        return await self._read(
            SessionClient.log_tail, batch_uuid, task_key, limit, session_id=session_id
        )

    @tool
    async def caches(self, session_id: str) -> list:
        """The caches of the session: entry states and value previews. The
        names feed load_cache_entries, clear_cache_entries and cache_value."""
        return await self._read(SessionClient.caches, session_id=session_id)

    @tool
    async def cache_value(
        self, session_id: str, cache_name: str, entry_name: str
    ) -> dict:
        """The rendered value of one cache entry."""
        return await self._read(
            SessionClient.cache_value, cache_name, entry_name, session_id=session_id
        )

    @tool
    async def apply_filter(
        self, session_id: str, query: str, scope: str = "tasks"
    ) -> list:
        """The identity keys the filter query matches. scope is 'tasks' for
        the roster or 'history' for the executed records."""
        return await self._read(
            SessionClient.apply_filter, query, scope=scope, session_id=session_id
        )

    @tool
    async def batch_options(self, session_id: str) -> dict:
        """The batch option baseline of the session. A run_tasks `options`
        dict overrides it per submit."""
        return await self._read(SessionClient.batch_options, session_id=session_id)

    @tool
    async def session_params(self, session_id: str) -> dict:
        """The configuration values the session runs with."""
        return await self._read(SessionClient.session_params, session_id=session_id)
