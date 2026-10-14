"""The MCP endpoint: one tool per port read and action, over the same
LocalAppClient the local TUI consumes (see winslow.client). The acks and
the refusals travel as tool results, so an agent reads every reason as
data. Blocking work runs on worker threads. Requires the [mcp] extra."""

import asyncio

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from winslow._meta import _tagged
from winslow.client.base import AppClient, SessionClient
from winslow.conf import settings
from winslow.exceptions import CredentialRefused, RequestError
from winslow.access.permissions import PermissionContext
from winslow.protocol.codec import CODEC
from winslow.task.context import scoped_permissions


class WinslowAccessToken(AccessToken):
    """The AccessToken of the SDK with the PermissionContext of the request
    on it. The SDK reads expires_at for its own expiry check, as an int
    second; the context keeps the float one."""

    context: PermissionContext


class BearerTokenVerifier(TokenVerifier):
    """The bearer token of one MCP request, through the same mode rule the
    websocket connection request runs (see CredentialMode.get_context). The
    token carries the whole context, so the tool call reads the stores once
    (see Authenticator.mcp_context)."""

    def __init__(self, mode):
        self.mode = mode

    async def verify_token(self, token):
        try:
            context = await asyncio.to_thread(self.mode.get_context, token)
        except CredentialRefused:
            return None
        return WinslowAccessToken(
            token=token,
            client_id=context.user,
            scopes=[],
            expires_at=None if context.expires_at is None else int(context.expires_at),
            context=context,
        )


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
        # The environment is in the identity of the server, so an agent with
        # several deployments tells them apart by name.
        self.server = MCPServer(
            name=f"{settings.SERVICE_NAME}-{settings.ENV}",
            instructions=(
                f"The live sessions of {settings.SERVICE_NAME} in the {settings.ENV} "
                "environment. list_sessions and "
                "descriptors show what runs and what can run; the read tools "
                "serve snapshots, history, logs and caches; the action tools "
                "answer with an ack: accepted, or refused with the reason."
            ),
            **self._auth_settings(base_url),
        )
        self._register_tools()

    def _auth_settings(self, base_url):
        authenticator = self.serve_app.authenticator
        if not authenticator.mode.checks_bearer:
            return {}
        # The resource metadata names the server an agent logs in at: the
        # issuer of the first listed backend that has one, else this server,
        # which issues nothing. The backends check the audience of a ticket
        # and an OIDC token themselves (SERVE_PUBLIC_URL), so the SDK does not
        # compare the resource.
        chain = authenticator.mode.chain
        issuer = next(
            (b.authorization_server for b in chain.backends if b.authorization_server),
            base_url,
        )
        return {
            "token_verifier": BearerTokenVerifier(authenticator.mode),
            "auth": AuthSettings(
                issuer_url=AnyHttpUrl(issuer),
                resource_server_url=AnyHttpUrl(f"{base_url}/mcp"),
                validate_token_resource=False,
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

    def close_listen_streams(self):
        """End every open subscriptions/listen stream with its final frame. A
        stream lives as long as its client, so the shutdown ends the streams
        before it waits for the connections (see ServeApp.close_streams)."""
        entry = self.server._lowlevel_server.get_request_handler("subscriptions/listen")
        if entry is not None:
            entry.handler.close()

    def _permissions(self):
        """The PermissionContext of the request (see Authenticator.mcp_context)."""
        return self.serve_app.authenticator.mcp_context(get_access_token())

    async def _submit(self, action, session_id=None):
        """The ack of the action as a dict (see ServeApp.submit). An unknown
        session id answers the refused ack shape, so an agent reads it as data."""
        try:
            with scoped_permissions(self._permissions()):
                ack = await self.serve_app.submit(action, session_id)
        except RequestError as exc:
            return {"accepted": False, "reason": str(exc)}
        return CODEC.dump(ack)

    async def _read(self, spec, *args, session_id=None, **kwargs):
        """One port read as a tool result (see ServeApp.read). A refusal
        answers {'error': reason}, so an agent reads it as data."""
        try:
            with scoped_permissions(self._permissions()):
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
            SessionClient.run_tasks.build(dict(keys=keys, options=options)), session_id
        )

    @tool
    async def check_tasks(
        self, session_id: str, keys: list[str], options: dict | None = None
    ) -> dict:
        """Submit a check batch for the given task identity keys. options
        works as in run_tasks."""
        return await self._submit(
            SessionClient.check_tasks.build(dict(keys=keys, options=options)),
            session_id,
        )

    @tool
    async def stop_batch(self, session_id: str, batch_uuid: str) -> dict:
        """Request a stop of one live batch."""
        return await self._submit(
            SessionClient.stop_batch.build(dict(batch_uuid=batch_uuid)), session_id
        )

    @tool
    async def end_session(self, session_id: str, force: bool = False) -> dict:
        """End the session. force stops its running batches first."""
        return await self._submit(
            SessionClient.end_session.build(dict(force=force)), session_id
        )

    @tool
    async def load_cache_entries(self, session_id: str, entries: list) -> dict:
        """Load the given cache entries. Each entry is a
        [cache_name, entry_name] pair (see caches)."""
        return await self._submit(
            SessionClient.load_cache_entries.build(dict(entries=entries)), session_id
        )

    @tool
    async def clear_cache_entries(self, session_id: str, entries: list) -> dict:
        """Clear the given cache entries. entries works as in
        load_cache_entries."""
        return await self._submit(
            SessionClient.clear_cache_entries.build(dict(entries=entries)), session_id
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
    async def host_stats(self) -> dict:
        """The resources of the host that runs the sessions: memory in
        bytes, the cpu load since the previous sample, hostname and user."""
        return await self._read(AppClient.host_stats)

    @tool
    async def start_session(
        self, workflow: str, overrides: dict | None = None, values: dict | None = None
    ) -> dict:
        """Create one live session of the named workflow. The ack carries its
        SessionInfo under `session`, or a refusal with the reason."""
        return await self._submit(
            AppClient.create_session.build(
                dict(workflow=workflow, overrides=overrides, values=values)
            )
        )

    @tool
    async def restore_session(self, session_id: str) -> dict:
        """Rebuild one dead session from its open manifest (see manifests).
        The ack carries its SessionInfo under `session`."""
        return await self._submit(
            AppClient.restore_session.build(dict(session_id=session_id))
        )

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
