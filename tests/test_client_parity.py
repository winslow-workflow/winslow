"""The session port, slice five: the parity harness. One scenario - create
a session, run tasks, load a cache entry, stop a batch, read the logs and
the history, end the session - runs twice, through the local adapter and
through the wire transport against a served process. The two DTO streams
must match: every read, every subscription lane, every ack, and the refusal
type (RequestError) with them. No TUI is in the loop.

The comparison maps the run-specific identifiers (session id, batch uuids,
timestamps, memory addresses) to stable placeholders. Event lanes that
interleave across worker threads compare per task key or as a multiset; the
per-key ladders stay ordered (see harness.py)."""

import logging
import re
import threading
from dataclasses import fields, is_dataclass
from enum import Enum
from functools import partial

from winslow.actions import EndSession, LoadCacheEntries, RunTasks, StopBatch
from winslow.client import LocalAppClient
from winslow.client.websocket import RemoteAppClient
from winslow.events import (
    BatchCompletedEvent,
    BatchCreatedEvent,
    ExecutionStatusEvent,
    LogLineEvent,
    SessionEndedEvent,
    TaskStatusEvent,
)
from winslow.exceptions import RequestError
from winslow.model import CacheUpdatedEvent, SessionLogEvent
from winslow.session import SessionRegistry
from winslow.task.status import TaskStatus

from harness import by_name

from test_client_websocket import TOKEN, ServedProcess, registered, wait_for
from test_serve_actions import serve_orchestrator

TOPICS = (
    TaskStatusEvent,
    ExecutionStatusEvent,
    BatchCreatedEvent,
    BatchCompletedEvent,
    LogLineEvent,
    SessionLogEvent,
    CacheUpdatedEvent,
    SessionEndedEvent,
)

# The volatile float fields of the DTOs: wall-clock stamps and durations.
TIME_KEYS = frozenset(
    {
        "created_at",
        "started_at",
        "completed_at",
        "checked_at",
        "written_at",
        "elapsed",
        "duration",
        "at",
    }
)

LOG_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC")
BATCH_UUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}")
ADDRESS = re.compile(r"0x[0-9a-f]+")
DECIMAL = re.compile(r"\d+\.\d+")


def normalize_text(text, names):
    """One string with every run-specific identifier replaced: the named ids
    first, then the generic timestamp, uuid, address and decimal shapes."""
    for real, placeholder in names.items():
        text = text.replace(real, placeholder)
    text = LOG_TIMESTAMP.sub("<ts>", text)
    text = BATCH_UUID.sub("<uuid>", text)
    text = ADDRESS.sub("<addr>", text)
    return DECIMAL.sub("<n>", text)


def canonical(value, names, key=None):
    """The comparable form of one port value: dataclasses become dicts that
    carry their class name, enums their names, sequences lists, strings and
    time-keyed floats their placeholder forms. The session log backlog
    sorts, because its worker threads interleave."""
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "__class__": type(value).__name__,
            **{
                f.name: canonical(getattr(value, f.name), names, f.name)
                for f in fields(value)
            },
        }
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, dict):
        return {
            canonical(k, names): canonical(v, names, k if isinstance(k, str) else None)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        items = [canonical(item, names, key) for item in value]
        return sorted(items) if key == "session_log_backlog" else items
    if isinstance(value, str):
        return normalize_text(value, names)
    if isinstance(value, float) and key in TIME_KEYS:
        return "ts"
    return value


class StreamRecorder:
    """The DTO stream of one drive: the reads by step name, the events by
    topic. The session end closes the stream, so both transports record up
    to the same cutoff (the serve bridge retires at the end frame)."""

    def __init__(self):
        self.reads = {}
        self.events = {topic: [] for topic in TOPICS}
        self.task_log_lines = []
        self.names = {}
        self._ended = threading.Event()

    def subscribe(self, client, task_key):
        for topic in TOPICS:
            client.subscribe(topic, partial(self.record, topic))
        return client.subscribe_task_log(task_key, self.record_task_log)

    def record(self, topic, event):
        if self._ended.is_set():
            return
        self.events[topic].append(event)
        if topic is SessionEndedEvent:
            self._ended.set()

    def record_task_log(self, event):
        if not self._ended.is_set():
            self.task_log_lines.append(event.line)

    def read(self, name, value):
        self.reads[name] = value

    def refusal(self, name, call):
        """Record the refusal of one read: the type and the served reason.
        A raise of any other type propagates and fails the drive."""
        try:
            call()
        except RequestError as exc:
            self.reads[name] = (type(exc).__name__, str(exc))
        else:
            self.reads[name] = ("no refusal", None)

    def gist_refusal(self, name, call):
        """The refusal record for a reason the transports word differently:
        the type must match, the reason only names the ended state."""
        try:
            call()
        except RequestError as exc:
            self.reads[name] = (type(exc).__name__, "has ended" in str(exc))
        else:
            self.reads[name] = ("no refusal", None)

    # --- the deterministic scenario checkpoints --------------------------------

    def wait_batch_completed(self, batch_uuid):
        wait_for(
            lambda: any(
                e.info.uuid == batch_uuid for e in self.events[BatchCompletedEvent]
            ),
            f"no batch_completed event for {batch_uuid}",
        )

    def wait_running(self, task_key, batch_uuid):
        wait_for(
            lambda: any(
                e.task_key == task_key
                and e.batch_uuid == batch_uuid
                and e.status is TaskStatus.RUNNING
                for e in self.events[ExecutionStatusEvent]
            ),
            "the gated task never reported RUNNING",
        )

    def wait_ended(self):
        assert self._ended.wait(10), "no session_ended event"


def gate_refresh(workflow, monkeypatch):
    """Hold the refresh task open on a gate event and log one line, so the
    scenario stops a verifiably busy batch and the log lanes carry content.
    The task class is shared between the drives, so the patch applies once."""
    task = next(
        t for t in workflow.tasks if t.identity_key.startswith("refresh-forecast")
    )
    cls = type(task)
    if getattr(cls, "_parity_gated", False):
        return
    original = cls.run

    def run(self):
        self.logger.warning("refresh holds the gate")
        self.target[("gate",)].wait(timeout=30)
        original(self)

    monkeypatch.setattr(cls, "run", run)
    monkeypatch.setattr(cls, "_parity_gated", True, raising=False)


def drive_scenario(app, registry, monkeypatch):
    """One full session through the port. The registry access parks the gate
    and opens the log level; everything the recorder keeps crossed the port."""
    recorder = StreamRecorder()
    recorder.read("descriptors", app.descriptors())
    recorder.read("manifests", app.manifests())
    row = app.create_session("my-cache")
    recorder.read("create_row", row)
    recorder.read("session_rows", app.sessions())
    client = app.session(row.session_id)
    workflow = registry.get(row.session_id).workflow
    # The cache lines are info level; the session log lane must carry them.
    workflow.logger.setLevel(logging.INFO)
    gate_refresh(workflow, monkeypatch)

    roster = client.roster()
    recorder.read("roster", roster)
    city_keys = tuple(i.key for i in roster if i.key.startswith("load-cities"))
    refresh_key = next(i.key for i in roster if i.key.startswith("refresh-forecast"))
    recorder.read("task_detail", client.task_detail(refresh_key))
    recorder.refusal("task_detail_unknown", lambda: client.task_detail("no-such-task"))

    recorder.read("task_log_backlog", recorder.subscribe(client, refresh_key))

    # Run tasks: the three parameterized city tasks in one batch.
    run_ack = client.submit(RunTasks(keys=city_keys))
    assert run_ack.accepted, run_ack.reason
    recorder.read("run_ack", run_ack)
    recorder.wait_batch_completed(run_ack.batch_uuid)
    recorder.read("snapshot_after_run", client.snapshot())

    # Cache loads: warm the lazy entry through the port action.
    recorder.read("cache_value_cold", client.cache_value("weather", "forecast"))
    load_ack = client.submit(LoadCacheEntries(entries=(("weather", "forecast"),)))
    assert load_ack.accepted, load_ack.reason
    recorder.read("load_ack", load_ack)
    wait_for(
        lambda: client.cache_value("weather", "forecast").state == "warm",
        "the load never warmed the entry",
    )
    recorder.read("cache_value_warm", client.cache_value("weather", "forecast"))
    recorder.read("caches_after_load", client.caches())
    recorder.refusal(
        "cache_value_unknown",
        lambda: client.cache_value("weather", "no-such-entry"),
    )

    # Stop: the gate holds the fourth task RUNNING until the stop lands.
    gate = threading.Event()
    workflow.target[("gate",)] = gate
    gated_ack = client.submit(RunTasks(keys=(refresh_key,)))
    assert gated_ack.accepted, gated_ack.reason
    recorder.read("gated_run_ack", gated_ack)
    recorder.wait_running(refresh_key, gated_ack.batch_uuid)
    recorder.read("stop_ack", client.submit(StopBatch(batch_uuid=gated_ack.batch_uuid)))
    recorder.read(
        "stop_unknown_ack", client.submit(StopBatch(batch_uuid="no-such-batch"))
    )
    gate.set()
    recorder.wait_batch_completed(gated_ack.batch_uuid)

    # Logs and history.
    recorder.read("log_tail", client.log_tail(gated_ack.batch_uuid, refresh_key))
    recorder.refusal(
        "log_tail_unknown",
        lambda: client.log_tail(gated_ack.batch_uuid, "no-such-task"),
    )
    recorder.read("history", client.history())
    recorder.read(
        "record_detail_completed",
        client.record_detail(run_ack.batch_uuid, city_keys[0]),
    )
    recorder.read(
        "record_detail_stopped",
        client.record_detail(gated_ack.batch_uuid, refresh_key),
    )
    recorder.refusal(
        "record_detail_unknown",
        lambda: client.record_detail("no-such-batch", refresh_key),
    )
    recorder.read("apply_filter", client.apply_filter("refresh"))
    recorder.refusal("apply_filter_broken", lambda: client.apply_filter("((broken"))
    recorder.read("batch_options", client.batch_options())
    recorder.read("session_params", client.session_params())

    # End, then the ended-session surface: the reads that survive, and the
    # refusals of the released state.
    end_ack = client.submit(EndSession())
    assert end_ack.accepted, end_ack.reason
    recorder.read("end_ack", end_ack)
    recorder.wait_ended()
    wait_for(lambda: client.snapshot().status == "ENDED", "the session never ENDED")
    recorder.read("snapshot_after_end", client.snapshot())
    recorder.read("session_rows_after_end", app.sessions())
    recorder.read("history_after_end", client.history())
    recorder.read(
        "apply_filter_history", client.apply_filter("refresh", scope="history")
    )
    recorder.refusal("roster_after_end", lambda: client.roster())
    recorder.refusal("task_detail_after_end", lambda: client.task_detail(refresh_key))
    recorder.gist_refusal("caches_after_end", lambda: client.caches())
    recorder.read("run_after_end_ack", client.submit(RunTasks(keys=(refresh_key,))))

    client.close()
    recorder.names = {
        row.session_id: "<session>",
        run_ack.batch_uuid: "<run-batch>",
        gated_ack.batch_uuid: "<gated-batch>",
    }
    return recorder


def by_task_key(events, key_of, value_of):
    grouped = {}
    for event in events:
        grouped.setdefault(key_of(event), []).append(value_of(event))
    return grouped


def canonical_stream(recorder):
    """The comparable form of one drive. The per-key ladders keep their
    order; the lanes that interleave across tasks or coalesce on the wire
    (the session log, the cache repaints) compare as multiset and set."""
    names = recorder.names
    events = recorder.events
    return {
        "reads": {
            name: canonical(value, names) for name, value in recorder.reads.items()
        },
        "task_status": by_task_key(
            events[TaskStatusEvent],
            lambda e: e.key,
            lambda e: (e.status.name, e.origin.name),
        ),
        "execution_status": by_task_key(
            events[ExecutionStatusEvent],
            lambda e: e.task_key,
            lambda e: (names.get(e.batch_uuid, "<uuid>"), e.status.name, e.origin.name),
        ),
        "batches_created": canonical([e.info for e in events[BatchCreatedEvent]], names),
        "batches_completed": canonical(
            [e.info for e in events[BatchCompletedEvent]], names
        ),
        "log_lines": by_task_key(
            events[LogLineEvent],
            lambda e: e.task_key,
            lambda e: (names.get(e.batch_uuid, "<uuid>"), normalize_text(e.line, names)),
        ),
        "session_log": sorted(
            normalize_text(e.line, names) for e in events[SessionLogEvent]
        ),
        "task_log": [normalize_text(line, names) for line in recorder.task_log_lines],
        "cache_updated": sorted({e.cache_name for e in events[CacheUpdatedEvent]}),
        "session_ended": canonical(events[SessionEndedEvent], names),
    }


def test_the_two_transports_serve_one_dto_stream(e2e_repo, state_store, monkeypatch):
    local_registry = SessionRegistry()
    local_app = LocalAppClient(
        local_registry,
        orchestrator=serve_orchestrator(e2e_repo),
        state_store=state_store,
    )
    local = canonical_stream(drive_scenario(local_app, local_registry, monkeypatch))

    serve_registry = SessionRegistry()
    process = ServedProcess(
        serve_registry,
        orchestrator=serve_orchestrator(e2e_repo),
        state_store=state_store,
    ).start()
    remote_app = RemoteAppClient(process.url, token=TOKEN).connect()
    try:
        remote = canonical_stream(
            drive_scenario(remote_app, serve_registry, monkeypatch)
        )
    finally:
        remote_app.close()
        process.stop()

    assert set(local["reads"]) == set(remote["reads"])
    for name in local["reads"]:
        assert local["reads"][name] == remote["reads"][name], f"read {name!r} diverges"
    for lane in local:
        if lane == "reads":
            continue
        assert local[lane] == remote[lane], f"event lane {lane!r} diverges"


# --- the pinned session-client lifecycle (spec slice 5) ---------------------------


def test_local_session_clients_are_per_call_and_close_is_scoped(e2e_repo):
    """LocalAppClient.session builds a fresh client per call, and close
    tears down only that client's subscriptions."""
    workflow, session, registry = registered(e2e_repo)
    app = LocalAppClient(registry)
    first = app.session(session.session_id)
    second = app.session(session.session_id)
    assert first is not second

    first_lines, second_lines = [], []
    first.subscribe(SessionLogEvent, first_lines.append)
    second.subscribe(SessionLogEvent, second_lines.append)
    first.close()
    workflow.logger.warning("after the first client closed")
    assert first_lines == []
    assert len(second_lines) == 1


def test_remote_session_clients_share_the_lane_and_close_drops_it(e2e_repo):
    """RemoteAppClient.session returns the shared lane of the session, and
    close tears down every handler of that session."""
    workflow, session, registry = registered(e2e_repo)
    process = ServedProcess(registry).start()
    client = RemoteAppClient(process.url, token=TOKEN).connect()
    try:
        lane = client.session(session.session_id)
        assert client.session(session.session_id) is lane

        first, second = [], []
        lane.subscribe(TaskStatusEvent, first.append)
        lane.subscribe(TaskStatusEvent, second.append)
        lane.close()
        fresh = client.session(session.session_id)
        assert fresh is not lane

        done = threading.Event()
        fresh.subscribe(BatchCompletedEvent, lambda e: done.set())
        alpha = by_name(workflow)["Alpha"]
        ack = fresh.submit(RunTasks(keys=(alpha.identity_key,)))
        assert ack.accepted, ack.reason
        assert done.wait(10), "no batch_completed on the fresh lane"
        assert first == []
        assert second == []
    finally:
        client.close()
        process.stop()
