"""The session-port protocol extensions, black-box through the websocket
wire: roster, caches, cache_value, cache_updated, record_detail, the
history and session row enrichments, batch_options and its changed event,
session_params, apply_filter, the session_log and task_log lanes,
manifests and restore_session, the descriptor parity fields, the
create_session error detail, the task_detail parity fix, the two cache
actions, and the inbound envelope validation that replaces a trusted
frame.get(...) read."""

import time

from winslow.constants import Mode
from winslow.serve import Credentials, create_app
from winslow.serve.app import PROTOCOL_VERSION
from winslow.serve.wire import Actions, Requests
from winslow.session import Session, SessionRegistry
from winslow.task.status import TaskStatus as S

from harness import build_workflow, by_name, wait_for_status

from test_serve_actions import connect, frames_until, registered, serve_orchestrator

TOKEN = "test-token"


def registered_workflow(e2e_repo, name, mode=Mode.TUI):
    workflow = build_workflow(e2e_repo, name, mode)
    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    registry = SessionRegistry()
    registry.register(session)
    return workflow, session, registry


def request(ws, request_id, kind, **fields):
    ws.send_json({"type": "request", "request_id": request_id, "kind": kind, **fields})
    return frames_until(ws, "result")


def action(ws, request_id, session_id, name, **fields):
    ws.send_json(
        {
            "type": "action",
            "request_id": request_id,
            "session_id": session_id,
            "action": name,
            "fields": fields,
        }
    )
    return frames_until(ws, "ack")


def wait_for_cache_value(ws, session_id, cache_name, entry_name, state, timeout=5.0):
    """Poll cache_value until state matches: the two cache actions ack as
    soon as they start (see ActionHandler._cache_entries_action), so a
    caller cannot assume the entry is done the moment the ack arrives."""
    deadline = time.monotonic() + timeout
    poll = 0
    while time.monotonic() < deadline:
        poll += 1
        result = request(
            ws, f"poll-{poll}", Requests.CACHE_VALUE, session_id=session_id,
            cache_name=cache_name, entry_name=entry_name,
        )
        if result["state"] == state:
            return result
        time.sleep(0.01)
    raise AssertionError(f"{cache_name}.{entry_name} never reached {state!r}")


# --- roster ------------------------------------------------------------------


def test_roster_serves_stub_task_info_in_launch_filter_order(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    ws = connect(registry)
    result = request(ws, "r-1", Requests.ROSTER, session_id=session.session_id)
    keys = [row["key"] for row in result["tasks"]]
    # Order, not just membership: the roster promises get_filtered_tasks order.
    assert keys == [t.identity_key for t in workflow.get_filtered_tasks()]
    # A stub: no full-capture fields.
    assert all(row["attributes"] is None for row in result["tasks"])
    ws.close()


# --- caches, cache_value, cache_updated, the two cache actions ---------------


def test_caches_serves_cards_with_entries_and_value_previews(e2e_repo):
    workflow, session, registry = registered_workflow(e2e_repo, "my-cache")
    ws = connect(registry)
    result = request(ws, "r-2", Requests.CACHES, session_id=session.session_id)
    (weather,) = [c for c in result["caches"] if c["name"] == "weather"]
    assert weather["scope"] == "workflow"
    entry_names = {e["name"] for e in weather["entries"]}
    assert entry_names == {"cities", "city_index", "forecast"}
    # Eager entries are warm at collection time; forecast is lazy and cold.
    assert "cities" in weather["values"]
    assert "forecast" not in weather["values"]
    ws.close()


def test_cache_value_renders_a_warm_entry_server_side(e2e_repo):
    workflow, session, registry = registered_workflow(e2e_repo, "my-cache")
    ws = connect(registry)
    result = request(
        ws, "r-3", Requests.CACHE_VALUE, session_id=session.session_id,
        cache_name="weather", entry_name="cities",
    )
    assert result["state"] == "warm"
    assert result["encoding"] == "text"
    assert "athens" in result["rendered"]
    ws.close()


def test_cache_value_reports_cold_with_no_value(e2e_repo):
    workflow, session, registry = registered_workflow(e2e_repo, "my-cache")
    ws = connect(registry)
    result = request(
        ws, "r-4", Requests.CACHE_VALUE, session_id=session.session_id,
        cache_name="weather", entry_name="forecast",
    )
    assert result["state"] == "cold"
    assert result["rendered"] is None
    ws.close()


def test_cache_value_refuses_an_unknown_entry(e2e_repo):
    workflow, session, registry = registered_workflow(e2e_repo, "my-cache")
    ws = connect(registry)
    ws.send_json(
        {
            "type": "request",
            "request_id": "r-5",
            "kind": Requests.CACHE_VALUE,
            "session_id": session.session_id,
            "cache_name": "weather",
            "entry_name": "nope",
        }
    )
    assert "has no entry 'nope'" in frames_until(ws, "error")["reason"]
    ws.close()


def test_load_cache_entries_action_computes_the_entry(e2e_repo):
    workflow, session, registry = registered_workflow(e2e_repo, "my-cache")
    ws = connect(registry)
    ack = action(
        ws, "r-6", session.session_id, Actions.LOAD_CACHE_ENTRIES,
        entries=[["weather", "forecast"]],
    )
    assert ack["accepted"] is True
    result = wait_for_cache_value(
        ws, session.session_id, "weather", "forecast", "warm"
    )
    assert "ATHENS" in result["rendered"]
    ws.close()


def test_clear_cache_entries_action_drops_the_entry_and_its_dependents(e2e_repo):
    workflow, session, registry = registered_workflow(e2e_repo, "my-cache")
    ws = connect(registry)
    ack = action(
        ws, "r-8", session.session_id, Actions.CLEAR_CACHE_ENTRIES,
        entries=[["weather", "cities"]],
    )
    assert ack["accepted"] is True
    wait_for_cache_value(ws, session.session_id, "weather", "cities", "cold")
    result = request(ws, "r-9", Requests.CACHES, session_id=session.session_id)
    (weather,) = [c for c in result["caches"] if c["name"] == "weather"]
    assert "cities" not in weather["values"]
    assert "city_index" not in weather["values"]
    ws.close()


def test_cache_entries_action_refuses_an_unknown_cache(e2e_repo):
    workflow, session, registry = registered_workflow(e2e_repo, "my-cache")
    ws = connect(registry)
    ack = action(
        ws, "r-10", session.session_id, Actions.LOAD_CACHE_ENTRIES,
        entries=[["nope", "cities"]],
    )
    assert ack["accepted"] is False
    assert "'nope' names no cache" in ack["reason"]
    ws.close()


def test_cache_entries_action_refuses_an_unknown_entry(e2e_repo):
    workflow, session, registry = registered_workflow(e2e_repo, "my-cache")
    ws = connect(registry)
    ack = action(
        ws, "r-42", session.session_id, Actions.LOAD_CACHE_ENTRIES,
        entries=[["weather", "nope"]],
    )
    assert ack["accepted"] is False
    assert "has no entry 'nope'" in ack["reason"]
    ws.close()


def test_cache_entries_action_refuses_an_empty_entries_list(e2e_repo):
    workflow, session, registry = registered_workflow(e2e_repo, "my-cache")
    ws = connect(registry)
    ack = action(
        ws, "r-43", session.session_id, Actions.LOAD_CACHE_ENTRIES, entries=[]
    )
    assert ack["accepted"] is False
    assert "entries list is empty" in ack["reason"]
    ws.close()


def test_clear_cache_entries_action_takes_a_multi_pair_list(e2e_repo):
    """The "clear all" case the spec singles out: one action, every visible
    pair, not one action per entry."""
    workflow, session, registry = registered_workflow(e2e_repo, "my-cache")
    ws = connect(registry)
    ack = action(
        ws, "r-44", session.session_id, Actions.CLEAR_CACHE_ENTRIES,
        entries=[["weather", "cities"], ["weather", "city_index"]],
    )
    assert ack["accepted"] is True
    wait_for_cache_value(ws, session.session_id, "weather", "cities", "cold")
    result = request(ws, "r-45", Requests.CACHES, session_id=session.session_id)
    (weather,) = [c for c in result["caches"] if c["name"] == "weather"]
    assert "cities" not in weather["values"]
    assert "city_index" not in weather["values"]
    ws.close()


def test_load_cache_entries_action_takes_a_multi_pair_list(e2e_repo):
    """The "load all" case: cities and forecast load together in one frame,
    independently of each other (forecast does not depend on the load of
    cities in this action, only on cities' own stored value)."""
    workflow, session, registry = registered_workflow(e2e_repo, "my-cache")
    ws = connect(registry)
    action(
        ws, "r-46", session.session_id, Actions.CLEAR_CACHE_ENTRIES,
        entries=[["weather", "cities"], ["weather", "forecast"]],
    )
    wait_for_cache_value(ws, session.session_id, "weather", "cities", "cold")
    wait_for_cache_value(ws, session.session_id, "weather", "forecast", "cold")

    ack = action(
        ws, "r-47", session.session_id, Actions.LOAD_CACHE_ENTRIES,
        entries=[["weather", "cities"], ["weather", "forecast"]],
    )
    assert ack["accepted"] is True
    wait_for_cache_value(ws, session.session_id, "weather", "cities", "warm")
    wait_for_cache_value(ws, session.session_id, "weather", "forecast", "warm")
    result = request(ws, "r-48", Requests.CACHES, session_id=session.session_id)
    (weather,) = [c for c in result["caches"] if c["name"] == "weather"]
    assert "cities" in weather["values"]
    assert "forecast" in weather["values"]
    ws.close()


def test_cache_updated_fires_on_a_live_invalidation(e2e_repo):
    workflow, session, registry = registered_workflow(e2e_repo, "my-cache")
    refresh = by_name(workflow)["RefreshForecast"]
    ws = connect(registry)
    ws.send_json({"type": "subscribe", "session_id": session.session_id})
    assert ws.receive_json()["type"] == "snapshot"

    action(ws, "r-11", session.session_id, Actions.RUN_TASKS, keys=[refresh.identity_key])
    frame = frames_until(ws, "cache_updated")
    assert frame["cache_name"] == "weather"
    ws.close()


def test_clear_cache_entries_action_itself_fires_cache_updated(e2e_repo):
    """cache_updated must fire from the action directly, not only as a side
    effect of a task run (see test_cache_updated_fires_on_a_live_invalidation,
    which drives invalidation through RUN_TASKS instead)."""
    workflow, session, registry = registered_workflow(e2e_repo, "my-cache")
    ws = connect(registry)
    ws.send_json({"type": "subscribe", "session_id": session.session_id})
    assert ws.receive_json()["type"] == "snapshot"

    action(
        ws, "r-49", session.session_id, Actions.CLEAR_CACHE_ENTRIES,
        entries=[["weather", "cities"]],
    )
    frame = frames_until(ws, "cache_updated")
    assert frame["cache_name"] == "weather"
    ws.close()


# --- record_detail, history tasks -----------------------------------------


def test_record_detail_serves_the_phase_timeline_and_snapshots(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    ws = connect(registry)
    ack = action(ws, "r-12", session.session_id, Actions.RUN_TASKS, keys=[alpha.identity_key])
    wait_for_status(workflow, alpha, S.COMPLETED)
    result = request(
        ws, "r-13", Requests.RECORD_DETAIL, session_id=session.session_id,
        batch_uuid=ack["batch_uuid"], task_key=alpha.identity_key,
    )
    assert result["info"]["key"] == alpha.identity_key
    assert result["phases"]
    assert all(p["phase"] and p["started_at"] for p in result["phases"])
    ws.close()


def test_history_rows_carry_started_at_duration_and_last_log_per_task(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    ws = connect(registry)
    action(ws, "r-14", session.session_id, Actions.RUN_TASKS, keys=[alpha.identity_key])
    wait_for_status(workflow, alpha, S.COMPLETED)
    result = request(ws, "r-15", Requests.HISTORY, session_id=session.session_id)
    (row,) = result["batches"]
    detail = row["tasks"][alpha.identity_key]
    assert detail["status"] == "COMPLETED"
    assert detail["started_at"] is not None
    assert detail["duration"] is not None
    ws.close()


# --- session rows -------------------------------------------------------------


def test_snapshot_session_rows_carry_display_and_progress_fields(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    app = create_app(
        registry, Credentials(token=TOKEN, require_credential=True), hello_timeout=1.0
    )
    from starlette.testclient import TestClient

    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "version": PROTOCOL_VERSION, "token": TOKEN})
        assert ws.receive_json()["type"] == "hello_ok"
        snapshot = ws.receive_json()
        (row,) = snapshot["sessions"]
        assert row["display_name"] == workflow.get_display_name()
        assert row["instance_name"] == workflow.instance_name
        assert row["started_at"] == session.start
        assert row["task_status_summary"]["total"] > 0


# --- batch_options and batch_options_changed -----------------------------------


def test_batch_options_request_serves_the_live_snapshot(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    ws = connect(registry)
    result = request(ws, "r-16", Requests.BATCH_OPTIONS, session_id=session.session_id)
    assert result["options"] == {
        "dry_run": workflow.dry_run,
        "force_run": workflow.force_run,
        "force_success": workflow.force_success,
        "disable_concurrency": workflow.disable_concurrency,
    }
    ws.close()


def test_set_batch_options_fires_the_changed_event(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    ws = connect(registry)
    ws.send_json({"type": "subscribe", "session_id": session.session_id})
    assert ws.receive_json()["type"] == "snapshot"

    action(ws, "r-17", session.session_id, Actions.SET_BATCH_OPTIONS, force_run=True)
    frame = frames_until(ws, "batch_options_changed")
    assert frame["options"]["force_run"] is True
    ws.close()


# --- session_params ------------------------------------------------------------


def test_session_params_serves_settings_and_resolved_config(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    ws = connect(registry)
    result = request(ws, "r-18", Requests.SESSION_PARAMS, session_id=session.session_id)
    assert result["settings"] == workflow.settings_snapshot
    assert set(result["workflow_config"]) == set(workflow.config_option_names)
    ws.close()


# --- apply_filter --------------------------------------------------------------


def test_apply_filter_serves_matching_identity_keys(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    ws = connect(registry)
    result = request(
        ws, "r-19", Requests.APPLY_FILTER, session_id=session.session_id, query="alpha"
    )
    assert result["keys"] == [alpha.identity_key]
    ws.close()


def test_apply_filter_answers_the_parse_error(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    ws = connect(registry)
    ws.send_json(
        {
            "type": "request",
            "request_id": "r-20",
            "kind": Requests.APPLY_FILTER,
            "session_id": session.session_id,
            "query": "((unclosed",
        }
    )
    error = frames_until(ws, "error")
    assert "r-20" == error["request_id"]
    ws.close()


def test_apply_filter_builtin_only_refuses_a_foreign_filter(e2e_repo, monkeypatch):
    from winslow.filter.builtin import GroupFilter

    monkeypatch.setattr("winslow.filter.builtin.BUILTIN_FILTERS", (GroupFilter,))
    workflow, session, registry = registered(e2e_repo)
    ws = connect(registry)
    ws.send_json(
        {
            "type": "request",
            "request_id": "r-21",
            "kind": Requests.APPLY_FILTER,
            "session_id": session.session_id,
            "query": "alpha",
            "builtin_only": True,
        }
    )
    error = frames_until(ws, "error")
    assert "supports only the builtin filters" in error["reason"]
    ws.close()


def test_apply_filter_history_scope_serves_record_keys_after_the_end(e2e_repo):
    """The history scope matches over the record infos, so a client with no
    parser of its own searches an ended session through the one endpoint."""
    workflow, session, registry = registered(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    ws = connect(registry)
    action(ws, "r-60", session.session_id, Actions.RUN_TASKS, keys=[alpha.identity_key])
    wait_for_status(workflow, alpha, S.COMPLETED)
    session.end()
    assert session.has_ended

    result = request(
        ws,
        "r-61",
        Requests.APPLY_FILTER,
        session_id=session.session_id,
        query="alpha",
        scope="history",
    )
    assert result["keys"] == [alpha.identity_key]

    # The tasks scope refuses the ended session with direction.
    ws.send_json(
        {
            "type": "request",
            "request_id": "r-62",
            "kind": Requests.APPLY_FILTER,
            "session_id": session.session_id,
            "query": "alpha",
        }
    )
    error = frames_until(ws, "error")
    assert "scope='history'" in error["reason"]
    ws.close()


# --- session_log and task_log lanes --------------------------------------------


def test_session_log_subscription_streams_the_workflow_logger(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    ws = connect(registry)
    ws.send_json({"type": "subscribe", "session_id": session.session_id})
    assert ws.receive_json()["type"] == "snapshot"

    # A non-debug orchestrator config keeps the console-facing logger at
    # WARNING; warning() is the level a session actually surfaces here.
    workflow.logger.warning("hello from the session logger")
    frame = frames_until(ws, "session_log_batch")
    assert any("hello from the session logger" in line for line in frame["lines"])
    ws.close()


def test_session_log_backlog_serves_lines_logged_before_any_subscribe(
    e2e_repo, state_store
):
    """The buffer attaches at create_session time, not at first subscribe:
    a line logged in between must still reach a client that subscribes
    later (see SessionLogBuffer)."""
    orchestrator = serve_orchestrator(e2e_repo)
    registry = SessionRegistry()
    ws = connect(registry, orchestrator=orchestrator, state_store=state_store)

    created = request(ws, "r-51", Requests.CREATE_SESSION, workflow="my-workflow")
    session = registry.get(created["session_id"])
    session.workflow.logger.warning("logged before anyone subscribed")

    ws.send_json({"type": "subscribe", "session_id": session.session_id})
    snapshot = ws.receive_json()
    assert snapshot["type"] == "snapshot"
    assert any(
        "logged before anyone subscribed" in line
        for line in snapshot["session_log_backlog"]
    )
    ws.close()


def test_subscribe_task_log_refuses_without_a_prior_session_subscribe(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    ws = connect(registry)
    ws.send_json(
        {
            "type": "subscribe_task_log",
            "request_id": "r-52",
            "session_id": session.session_id,
            "task_key": alpha.identity_key,
        }
    )
    error = frames_until(ws, "error")
    assert error["request_id"] == "r-52"
    assert "subscribe" in error["reason"]

    # The connection is still healthy: subscribing properly now works.
    ws.send_json({"type": "subscribe", "session_id": session.session_id})
    assert ws.receive_json()["type"] == "snapshot"
    ws.close()


def test_task_log_subscription_serves_backlog_then_live_lines(e2e_repo, monkeypatch):
    workflow, session, registry = registered(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    original = type(alpha).run

    def run(self):
        self.logger.warning("alpha task-log hello")
        original(self)

    monkeypatch.setattr(type(alpha), "run", run)

    ws = connect(registry)
    ws.send_json({"type": "subscribe", "session_id": session.session_id})
    assert ws.receive_json()["type"] == "snapshot"

    ws.send_json(
        {
            "type": "subscribe_task_log",
            "request_id": "tl-1",
            "session_id": session.session_id,
            "task_key": alpha.identity_key,
        }
    )
    backlog = frames_until(ws, "task_log_backlog")
    assert backlog["lines"] == []

    action(ws, "r-22", session.session_id, Actions.RUN_TASKS, keys=[alpha.identity_key])
    frame = frames_until(ws, "task_log_batch")
    assert frame["task_key"] == alpha.identity_key
    assert any("alpha task-log hello" in line for line in frame["lines"])

    ws.send_json(
        {
            "type": "unsubscribe_task_log",
            "session_id": session.session_id,
            "task_key": alpha.identity_key,
        }
    )
    unsub = frames_until(ws, "unsubscribed_task_log")
    assert unsub["task_key"] == alpha.identity_key
    ws.close()


# --- manifests and restore_session ------------------------------------------


def test_manifests_and_restore_session_round_trip(e2e_repo, state_store):
    orchestrator = serve_orchestrator(e2e_repo)
    registry = SessionRegistry()
    ws = connect(registry, orchestrator=orchestrator, state_store=state_store)

    created = request(ws, "r-23", Requests.CREATE_SESSION, workflow="my-workflow")
    session_id = created["session_id"]

    # Simulate a dead process: the session drops out of this registry, but
    # its manifest stays open (never marked ended).
    registry.remove(session_id)

    manifests = request(ws, "r-24", Requests.MANIFESTS)
    (row,) = [m for m in manifests["manifests"] if m["session_id"] == session_id]
    assert row["workflow_class"] == "my-workflow"

    restored = request(ws, "r-25", Requests.RESTORE_SESSION, session_id=session_id)
    assert restored["session_id"] == session_id
    assert restored["status"] == "ACTIVE"
    assert session_id in registry
    ws.close()


def test_restore_session_refuses_an_unknown_manifest(e2e_repo, state_store):
    orchestrator = serve_orchestrator(e2e_repo)
    ws = connect(SessionRegistry(), orchestrator=orchestrator, state_store=state_store)
    ws.send_json(
        {
            "type": "request",
            "request_id": "r-26",
            "kind": Requests.RESTORE_SESSION,
            "session_id": "gone",
        }
    )
    error = frames_until(ws, "error")
    assert "names no open manifest" in error["reason"]
    ws.close()


# --- descriptor parity: action, const, initial ----------------------------------


def test_descriptor_option_rows_carry_action_const_and_initial(e2e_repo):
    orchestrator = serve_orchestrator(e2e_repo)
    ws = connect(SessionRegistry(), orchestrator=orchestrator)
    result = request(ws, "r-27", Requests.DESCRIPTORS)
    (dry_run,) = [o for o in result["overrides"] if o["name"] == "dry_run"]
    assert dry_run["action"] == "store_true"
    assert "initial" in dry_run
    ws.close()


def test_workflow_option_initial_prefills_from_cli_args(e2e_repo):
    """A workflow option's `initial` matches the local start form: the value
    the serve process's own CLI invocation supplied, not the class default
    (see Orchestrator.collect_workflow_args)."""
    orchestrator = serve_orchestrator(e2e_repo, "--client", "acme")
    ws = connect(SessionRegistry(), orchestrator=orchestrator)
    result = request(ws, "r-34", Requests.DESCRIPTORS)
    identified = next(
        row for row in result["workflows"] if row["workflow"] == "my-identified"
    )
    (client,) = [o for o in identified["options"] if o["name"] == "client"]
    assert client["initial"] == "acme"
    ws.close()


# --- create_session error detail -------------------------------------------------


def test_create_session_error_carries_a_traceback_detail(e2e_repo, state_store):
    orchestrator = serve_orchestrator(e2e_repo)
    ws = connect(SessionRegistry(), orchestrator=orchestrator, state_store=state_store)
    ws.send_json(
        {
            "type": "request",
            "request_id": "r-28",
            "kind": Requests.CREATE_SESSION,
            "workflow": "my-identified",
        }
    )
    error = frames_until(ws, "error")
    assert "requires client" in error["reason"]
    assert "Traceback" in error["detail"]
    ws.close()


# --- task_detail parity: workflow.task_info fills checked_at ---------------------


def test_task_detail_fills_checked_at_from_the_snapshot(e2e_repo, state_store):
    workflow = build_workflow(e2e_repo, "my-workflow", Mode.TUI)
    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    # checked_at comes from the persisted snapshot (see Workflow.task_info);
    # persistence starts only once the pipeline is runnable.
    workflow.init_state(state_store, origin="test")
    registry = SessionRegistry()
    registry.register(session)
    alpha = by_name(workflow)["Alpha"]
    ws = connect(registry)
    action(ws, "r-29", session.session_id, Actions.RUN_TASKS, keys=[alpha.identity_key])
    wait_for_status(workflow, alpha, S.COMPLETED)
    result = request(
        ws, "r-30", Requests.TASK_DETAIL, session_id=session.session_id,
        task_key=alpha.identity_key,
    )
    assert result["info"]["checked_at"] is not None
    ws.close()


# --- envelope validation at the serve edge ---------------------------------------


def test_a_malformed_action_frame_answers_an_error_and_the_connection_survives(
    e2e_repo,
):
    workflow, session, registry = registered(e2e_repo)
    ws = connect(registry)
    ws.send_json(
        {"type": "action", "request_id": "r-31", "action": Actions.RUN_TASKS}
    )
    error = frames_until(ws, "error")
    assert error["request_id"] == "r-31"
    assert "malformed" in error["reason"]

    # The connection is still alive: a valid frame answers normally.
    result = request(
        ws, "r-32", Requests.BATCH_OPTIONS, session_id=session.session_id
    )
    assert result["request_id"] == "r-32"
    ws.close()


def test_a_request_frame_naming_no_kind_answers_names_no_request(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    ws = connect(registry)
    ws.send_json({"type": "request", "request_id": "r-33"})
    error = frames_until(ws, "error")
    assert error["request_id"] == "r-33"
    assert "names no request" in error["reason"]
    ws.close()


def test_a_request_frame_missing_a_required_field_answers_malformed(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    ws = connect(registry)
    ws.send_json({"type": "request", "kind": Requests.LOG_TAIL, "request_id": "r-34"})
    error = frames_until(ws, "error")
    assert error["request_id"] == "r-34"
    assert "malformed" in error["reason"]
    ws.close()


def test_a_valid_json_non_dict_frame_answers_an_error_and_the_connection_survives(
    e2e_repo,
):
    workflow, session, registry = registered(e2e_repo)
    ws = connect(registry)
    ws.send_json([1, 2])
    error = frames_until(ws, "error")
    assert "must be a JSON object" in error["reason"]

    # The connection is still alive: a valid frame answers normally.
    result = request(
        ws, "r-35", Requests.BATCH_OPTIONS, session_id=session.session_id
    )
    assert result["request_id"] == "r-35"
    ws.close()


def test_an_unhashable_session_id_on_a_subscribe_frame_answers_an_error(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    ws = connect(registry)
    ws.send_json({"type": "subscribe", "session_id": ["not", "a", "string"]})
    error = frames_until(ws, "error")
    assert "malformed" in error["reason"]

    # The connection is still alive: a valid frame answers normally.
    ws.send_json({"type": "subscribe", "session_id": session.session_id})
    assert ws.receive_json()["type"] == "snapshot"
    ws.close()


def test_an_unhashable_session_id_on_a_task_log_frame_answers_an_error(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    ws = connect(registry)
    ws.send_json(
        {
            "type": "subscribe_task_log",
            "session_id": {"not": "a string"},
            "task_key": "whatever",
        }
    )
    error = frames_until(ws, "error")
    assert "malformed" in error["reason"]
    ws.close()


def test_a_malformed_unsubscribe_frame_answers_an_error(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    ws = connect(registry)
    ws.send_json({"type": "unsubscribe"})
    error = frames_until(ws, "error")
    assert "malformed" in error["reason"]
    ws.close()


# --- ended sessions answer directional errors, not a generic 500 ------------------


def test_the_live_session_reads_answer_directional_errors_once_ended(e2e_repo):
    workflow, session, registry = registered_workflow(e2e_repo, "my-cache")
    ws = connect(registry)
    session.end()
    assert session.has_ended

    for kind, fields in [
        (Requests.ROSTER, {}),
        (Requests.CACHES, {}),
        (Requests.CACHE_VALUE, {"cache_name": "weather", "entry_name": "cities"}),
        (Requests.APPLY_FILTER, {"query": "alpha"}),
    ]:
        ws.send_json(
            {
                "type": "request",
                "request_id": f"ended-{kind}",
                "kind": kind,
                "session_id": session.session_id,
                **fields,
            }
        )
        error = frames_until(ws, "error")
        assert error["request_id"] == f"ended-{kind}"
        assert "has ended" in error["reason"]
    ws.close()


def test_task_detail_answers_a_directional_error_once_ended(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    ws = connect(registry)
    session.end()
    ws.send_json(
        {
            "type": "request",
            "request_id": "r-36",
            "kind": Requests.TASK_DETAIL,
            "session_id": session.session_id,
            "task_key": alpha.identity_key,
        }
    )
    error = frames_until(ws, "error")
    assert "has ended" in error["reason"]
    ws.close()


def test_subscribe_task_log_answers_a_directional_error_once_ended(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    ws = connect(registry)
    ws.send_json({"type": "subscribe", "session_id": session.session_id})
    assert ws.receive_json()["type"] == "snapshot"
    session.end()
    ws.send_json(
        {
            "type": "subscribe_task_log",
            "request_id": "r-37",
            "session_id": session.session_id,
            "task_key": alpha.identity_key,
        }
    )
    error = frames_until(ws, "error")
    assert "has ended" in error["reason"]
    ws.close()


def test_history_log_tail_and_record_detail_still_serve_an_ended_session(e2e_repo):
    """The five handlers on requires_session (not requires_live_session)
    keep working after end: they read the record store and workflow
    attributes that survive release_tasks."""
    workflow, session, registry = registered(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    ws = connect(registry)
    ack = action(
        ws, "r-38", session.session_id, Actions.RUN_TASKS, keys=[alpha.identity_key]
    )
    wait_for_status(workflow, alpha, S.COMPLETED)
    session.end()
    assert session.has_ended

    history = request(ws, "r-39", Requests.HISTORY, session_id=session.session_id)
    assert history["batches"]

    log_tail = request(
        ws, "r-40", Requests.LOG_TAIL, session_id=session.session_id,
        batch_uuid=ack["batch_uuid"], task_key=alpha.identity_key,
    )
    assert "lines" in log_tail

    record_detail = request(
        ws, "r-41", Requests.RECORD_DETAIL, session_id=session.session_id,
        batch_uuid=ack["batch_uuid"], task_key=alpha.identity_key,
    )
    assert record_detail["info"]["key"] == alpha.identity_key
    ws.close()
