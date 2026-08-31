"""The port contract, parameterized over the transports: the same black-box
tests drive the local adapter, the wire client and the MCP door. A future
transport joins by adding a fixture param and an adapter with the same
surface. MCP has no event lane, so the contract here is reads and actions;
the event parity lives in test_client_parity."""

import asyncio
import json
import threading
import time
from dataclasses import asdict

import pytest

from winslow.actions import (
    Ack,
    BatchAck,
    ClearCacheEntries,
    EndSession,
    LoadCacheEntries,
    RunTasks,
)
from winslow.client import LocalAppClient
from winslow.client.websocket import ACTION_NAMES, RemoteAppClient
from winslow.codec import CODEC
from winslow.exceptions import RequestError
from winslow.model import (
    CacheCard,
    CacheValueView,
    HistoryRow,
    ManifestRow,
    RecordDetail,
    SessionParams,
    SessionRow,
    SessionSnapshot,
    TaskInfo,
)
from winslow.session import SessionRegistry

from test_client_websocket import TOKEN, ServedProcess
from test_serve_actions import serve_orchestrator


def wait_for(predicate, message, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(message)


# --- the MCP adapter ----------------------------------------------------------


class McpBridge:
    """One MCP client session on a background loop, callable from sync test
    code the way the wire client is."""

    def __init__(self, url, token):
        self._url = url
        self._token = token
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._ready = threading.Event()
        self._stop = None
        self._session = None
        self._runner = None

    def start(self):
        self._thread.start()
        self._runner = asyncio.run_coroutine_threadsafe(self._run(), self._loop)
        if not self._ready.wait(timeout=15) or self._session is None:
            raise AssertionError("the MCP session never initialized")
        return self

    async def _run(self):
        import httpx2
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        self._stop = asyncio.Event()
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            async with httpx2.AsyncClient(headers=headers) as http:
                async with streamable_http_client(
                    self._url, http_client=http
                ) as (read_stream, write_stream, *_):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        self._session = session
                        self._ready.set()
                        await self._stop.wait()
        finally:
            self._ready.set()

    def call(self, tool, args):
        future = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(tool, args), self._loop
        )
        return _unwrap(future.result(timeout=30))

    def close(self):
        self._loop.call_soon_threadsafe(self._stop.set)
        try:
            self._runner.result(timeout=10)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=10)


def _unwrap(outcome):
    """The tool result as plain data. The SDK renders a list return as one
    item per element; a lone item unwraps, so _aslist rewraps at the reads
    that answer lists."""
    if outcome.structured_content is not None:
        return outcome.structured_content

    def value(item):
        try:
            return json.loads(item.text)
        except json.JSONDecodeError:
            return item.text

    items = [value(item) for item in outcome.content]
    return items[0] if len(items) == 1 else items


def _aslist(result):
    if isinstance(result, list):
        return result
    return [result]


def _refuses(result):
    if isinstance(result, dict) and set(result) == {"error"}:
        raise RequestError(result["error"])
    return result


class McpSessionPort:
    """The SessionClient reads and actions over the MCP tools, decoding back
    into the model DTOs the way the wire client decodes frames."""

    def __init__(self, bridge, session_id):
        self._bridge = bridge
        self.session_id = session_id

    def _call(self, tool, **args):
        return _refuses(
            self._bridge.call(tool, {"session_id": self.session_id, **args})
        )

    def snapshot(self):
        return CODEC.decode(SessionSnapshot, self._call("snapshot"))

    def roster(self):
        return tuple(
            CODEC.decode(TaskInfo, row) for row in _aslist(self._call("roster"))
        )

    def task_detail(self, key):
        return CODEC.decode(TaskInfo, self._call("task_detail", task_key=key))

    def record_detail(self, batch_uuid, key):
        return CODEC.decode(
            RecordDetail,
            self._call("record_detail", batch_uuid=batch_uuid, task_key=key),
        )

    def history(self):
        return tuple(
            CODEC.decode(HistoryRow, row) for row in _aslist(self._call("history"))
        )

    def log_tail(self, batch_uuid, key, limit=200):
        return [
            str(line)
            for line in _aslist(
                self._call(
                    "log_tail", batch_uuid=batch_uuid, task_key=key, limit=limit
                )
            )
        ]

    def caches(self):
        return tuple(
            CODEC.decode(CacheCard, card) for card in _aslist(self._call("caches"))
        )

    def cache_value(self, cache_name, entry_name):
        return CODEC.decode(
            CacheValueView,
            self._call("cache_value", cache_name=cache_name, entry_name=entry_name),
        )

    def apply_filter(self, query, scope="tasks"):
        return tuple(_aslist(self._call("apply_filter", query=query, scope=scope)))

    def batch_options(self):
        return dict(self._call("batch_options"))

    def session_params(self):
        return CODEC.decode(SessionParams, self._call("session_params"))

    def submit(self, action):
        result = self._bridge.call(
            ACTION_NAMES[type(action)],
            {"session_id": self.session_id, **asdict(action)},
        )
        if "batch_uuid" in result:
            return BatchAck(
                accepted=result["accepted"],
                reason=result["reason"],
                batch_uuid=result["batch_uuid"],
            )
        return Ack(accepted=result["accepted"], reason=result.get("reason"))


class McpAppPort:
    """The AppClient reads over the MCP tools (see McpSessionPort)."""

    def __init__(self, bridge):
        self._bridge = bridge

    def sessions(self):
        return tuple(
            CODEC.decode(SessionRow, row)
            for row in _aslist(_refuses(self._bridge.call("list_sessions", {})))
        )

    def manifests(self):
        return tuple(
            CODEC.decode(ManifestRow, row)
            for row in _aslist(_refuses(self._bridge.call("manifests", {})))
        )

    def create_session(self, workflow, overrides=None, values=None):
        row = _refuses(
            self._bridge.call(
                "start_session",
                {"workflow": workflow, "overrides": overrides, "values": values},
            )
        )
        return CODEC.decode(SessionRow, row)

    def restore_session(self, session_id):
        row = _refuses(
            self._bridge.call("restore_session", {"session_id": session_id})
        )
        return CODEC.decode(SessionRow, row)

    def session(self, session_id):
        return McpSessionPort(self._bridge, session_id)


# --- the transport fixture ------------------------------------------------------


class McpServedProcess(ServedProcess):
    """A served process with the MCP door mounted next to the websocket."""

    def __init__(self, registry, orchestrator=None, state_store=None):
        from winslow.serve import Credentials, create_app

        self.app = create_app(
            registry,
            Credentials(token=TOKEN, require_credential=True),
            hello_timeout=2.0,
            orchestrator=orchestrator,
            state_store=state_store,
            mcp=True,
            base_url="http://127.0.0.1",
        )
        self.server = None
        self.thread = None
        self.port = 0


@pytest.fixture(params=["local", "ws", "mcp"])
def port(request, e2e_repo, state_store):
    registry = SessionRegistry()
    orchestrator = serve_orchestrator(e2e_repo)
    if request.param == "local":
        yield LocalAppClient(
            registry, orchestrator=orchestrator, state_store=state_store
        )
        return
    process = McpServedProcess(
        registry, orchestrator=orchestrator, state_store=state_store
    ).start()
    if request.param == "ws":
        client = RemoteAppClient(process.url, token=TOKEN).connect()
        try:
            yield client
        finally:
            client.close()
            process.stop()
        return
    bridge = McpBridge(f"http://127.0.0.1:{process.port}/mcp", TOKEN).start()
    try:
        yield McpAppPort(bridge)
    finally:
        bridge.close()
        process.stop()


def finished_batch(lane, batch_uuid):
    def check():
        row = next((r for r in lane.history() if r.uuid == batch_uuid), None)
        return row if row is not None and row.status == "FINISHED" else None

    return wait_for(check, f"batch {batch_uuid} never finished")


# --- the contract ---------------------------------------------------------------


def test_a_created_session_lists_and_snapshots(port):
    row = port.create_session("my-workflow")
    assert row.status == "ACTIVE"
    assert row.session_id in {r.session_id for r in port.sessions()}
    snapshot = port.session(row.session_id).snapshot()
    assert snapshot.session_id == row.session_id
    assert snapshot.tasks


def test_an_unknown_workflow_refuses_with_its_name(port):
    with pytest.raises(RequestError, match="no-such-workflow"):
        port.create_session("no-such-workflow")


def test_a_run_completes_and_serves_its_records(port):
    row = port.create_session("my-workflow")
    lane = port.session(row.session_id)
    key = next(info.key for info in lane.roster() if info.key.startswith("alpha"))

    ack = lane.submit(RunTasks(keys=(key,)))
    assert ack.accepted, ack.reason
    batch = finished_batch(lane, ack.batch_uuid)

    assert batch.tasks[key].status == "COMPLETED"
    assert lane.snapshot().tasks[key] == "COMPLETED"
    detail = lane.record_detail(ack.batch_uuid, key)
    assert detail.info.key == key
    assert isinstance(lane.log_tail(ack.batch_uuid, key), list)


def test_the_task_surface_serves_detail_and_filter(port):
    row = port.create_session("my-workflow")
    lane = port.session(row.session_id)
    key = next(info.key for info in lane.roster() if info.key.startswith("alpha"))
    assert lane.task_detail(key).key == key
    assert key in lane.apply_filter("alpha")
    assert lane.batch_options()["dry_run"] is False
    assert lane.session_params() is not None


def test_the_cache_surface_loads_and_clears(port):
    row = port.create_session("my-cache")
    lane = port.session(row.session_id)
    (card,) = [c for c in lane.caches() if c.name == "weather"]
    assert {entry.name for entry in card.entries} == {
        "cities",
        "city_index",
        "forecast",
    }

    ack = lane.submit(LoadCacheEntries(entries=(("weather", "forecast"),)))
    assert ack.accepted, ack.reason
    wait_for(
        lambda: lane.cache_value("weather", "forecast").state == "warm",
        "the forecast entry never warmed",
    )
    assert "ATHENS" in lane.cache_value("weather", "forecast").rendered

    ack = lane.submit(ClearCacheEntries(entries=(("weather", "forecast"),)))
    assert ack.accepted, ack.reason
    wait_for(
        lambda: lane.cache_value("weather", "forecast").state == "cold",
        "the forecast entry never cleared",
    )


def test_an_ended_session_refuses_the_live_surface(port):
    row = port.create_session("my-workflow")
    lane = port.session(row.session_id)
    ack = lane.submit(EndSession())
    assert ack.accepted, ack.reason
    wait_for(
        lambda: next(
            (r for r in port.sessions() if r.session_id == row.session_id), None
        ).status
        == "ENDED",
        "the session never ended",
    )
    refused = lane.submit(RunTasks(keys=("anything",)))
    assert refused.accepted is False
    assert "has ended" in refused.reason
    with pytest.raises(RequestError, match="has ended"):
        lane.roster()


def test_restore_refuses_an_unknown_manifest(port):
    assert port.manifests() == ()
    with pytest.raises(RequestError, match="no open manifest"):
        port.restore_session("gone")
