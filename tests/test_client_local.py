"""The session port, slice two: the local adapter hands core-built
dataclasses through. Each read answers the same DTO the core state implies,
the subscriptions relay the bus payloads, and the app scope creates and
restores sessions through the shared create_session flow."""

import time
from dataclasses import asdict

import pytest

from winslow.actions import (
    BatchAck,
    CheckTasks,
    ClearCacheEntries,
    EndSession,
    LoadCacheEntries,
    RunTasks,
    StopBatch,
)
from winslow.client import LocalAppClient, LocalSessionClient
from winslow.constants import Mode
from winslow.events import (
    BatchCompletedEvent,
    BatchCreatedEvent,
    ExecutionStatusEvent,
    LogLineEvent,
    SessionEndedEvent,
    TaskStatusEvent,
)
from winslow.exceptions import MisconfigurationError, RequestError
from winslow.model import (
    BatchInfo,
    CacheCard,
    CacheUpdatedEvent,
    CacheValueView,
    HistoryRow,
    RecordDetail,
    SessionLogEvent,
    SessionParams,
    SessionSnapshot,
)
from winslow.orchestrator import Orchestrator, OrchestratorConfig
from winslow.runner.execution import ExecutionStatus
from winslow.session import Session, SessionRegistry

from harness import build_workflow, by_name, gated_workflow, start_gated_batch


def registered(e2e_repo, name="my-workflow", mode=Mode.TUI):
    workflow = build_workflow(e2e_repo, name, mode)
    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    registry = SessionRegistry()
    registry.register(session)
    return workflow, session, registry


def session_client(e2e_repo, name="my-workflow"):
    workflow, session, registry = registered(e2e_repo, name)
    return workflow, session, LocalSessionClient(session)


def local_orchestrator(e2e_repo, *workflow_argv):
    """workflow_argv passes workflow options the way the CLI does, so the
    parsed base of collect_workflow_args carries them."""
    config, unknown = Orchestrator.get_base_parser().parse_known_args(
        ["serve", *workflow_argv], namespace=OrchestratorConfig()
    )
    orchestrator = Orchestrator(config, directory=e2e_repo, unknown_args=unknown)
    orchestrator.workflow_registry.collect_classes(e2e_repo)
    return orchestrator


def run_to_completion(workflow, client, task):
    """Submit one run through the port and block until the batch drains."""
    ack = client.submit(RunTasks(keys=(task.identity_key,)))
    assert ack.accepted, ack.reason
    workflow.runner.get_batch(ack.batch_uuid).wait()
    return ack


def wait_for(predicate, message, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError(message)


# --- the app scope -------------------------------------------------------------


def test_sessions_serves_a_row_per_live_session(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    app = LocalAppClient(registry)
    (row,) = app.sessions()
    assert row.session_id == session.session_id
    assert row.workflow == str(workflow)
    assert row.status == "ACTIVE"
    assert row.display_name == workflow.get_display_name()
    assert row.instance_name == workflow.instance_name
    # The server root travels with the row, so a client shortens the
    # server source paths (see TaskDetailRenderContext.root_dir).
    assert row.root_dir == str(e2e_repo)
    completed, problematic, total = session.task_status_summary
    assert row.task_status_summary.total == total
    assert row.task_status_summary.completed == completed


def test_descriptors_names_the_collected_workflows_and_overrides(e2e_repo):
    app = LocalAppClient(SessionRegistry(), orchestrator=local_orchestrator(e2e_repo))
    descriptors = app.descriptors()
    workflows = {d.workflow for d in descriptors.workflows}
    assert "my-workflow" in workflows
    override_names = {row.name for row in descriptors.overrides}
    assert "force_run" in override_names


def test_app_reads_refuse_without_their_dependencies(e2e_repo):
    app = LocalAppClient(SessionRegistry())
    with pytest.raises(MisconfigurationError, match="orchestrator"):
        app.descriptors()
    with pytest.raises(MisconfigurationError, match="state store"):
        app.manifests()
    with pytest.raises(MisconfigurationError, match="orchestrator"):
        app.create_session("my-workflow")


def test_create_session_registers_and_stamps_a_local_manifest(
    e2e_repo, state_store
):
    registry = SessionRegistry()
    app = LocalAppClient(
        registry, orchestrator=local_orchestrator(e2e_repo), state_store=state_store
    )
    row = app.create_session("my-workflow")
    assert row.session_id in registry
    assert row.status == "ACTIVE"
    manifest = state_store.load_manifest(row.session_id)
    assert manifest.origin == "local"

    # The log buffer attaches at creation, so the snapshot backlog carries
    # lines logged before any subscriber existed.
    session = registry.get(row.session_id)
    session.workflow.logger.warning("logged before anyone subscribed")
    snapshot = app.session(row.session_id).snapshot()
    assert any(
        "logged before anyone subscribed" in line
        for line in snapshot.session_log_backlog
    )


def test_create_session_refuses_an_unknown_workflow(e2e_repo, state_store):
    app = LocalAppClient(
        SessionRegistry(),
        orchestrator=local_orchestrator(e2e_repo),
        state_store=state_store,
    )
    with pytest.raises(RequestError, match="names no collected workflow"):
        app.create_session("no-such-workflow")


def test_manifests_and_restore_round_trip(e2e_repo, state_store):
    registry = SessionRegistry()
    app = LocalAppClient(
        registry, orchestrator=local_orchestrator(e2e_repo), state_store=state_store
    )
    row = app.create_session("my-workflow")
    session_id = row.session_id

    # A live session is no restore candidate, and restoring it is refused.
    assert session_id not in {m.session_id for m in app.manifests()}
    with pytest.raises(RequestError, match="already a live session"):
        app.restore_session(session_id)

    # Simulate a dead process: the session drops out of the registry, but
    # its manifest stays open.
    registry.remove(session_id)
    (manifest,) = [m for m in app.manifests() if m.session_id == session_id]
    assert manifest.workflow_class == "my-workflow"

    restored = app.restore_session(session_id)
    assert restored.session_id == session_id
    assert restored.status == "ACTIVE"
    assert session_id in registry


def test_restore_refuses_an_unknown_manifest(e2e_repo, state_store):
    app = LocalAppClient(
        SessionRegistry(),
        orchestrator=local_orchestrator(e2e_repo),
        state_store=state_store,
    )
    with pytest.raises(RequestError, match="names no open manifest"):
        app.restore_session("gone")


def test_session_resolves_a_session_client(e2e_repo):
    workflow, session, registry = registered(e2e_repo)
    app = LocalAppClient(registry)
    client = app.session(session.session_id)
    assert client.session_id == session.session_id
    with pytest.raises(KeyError, match="does not resolve to a live session"):
        app.session("gone")


# --- the reads ------------------------------------------------------------------


def test_snapshot_equals_the_core_state(e2e_repo):
    workflow, session, client = session_client(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    ack = run_to_completion(workflow, client, alpha)

    snapshot = client.snapshot()
    # The whole DTO, not field spot checks: the port serves exactly what the
    # core state implies.
    assert snapshot == SessionSnapshot.from_session(session)
    assert snapshot.session_id == session.session_id
    assert snapshot.workflow == str(workflow)
    assert snapshot.status == "ACTIVE"
    assert snapshot.tasks == {
        key: status.name for key, status in workflow.store.items()
    }
    (batch_row,) = snapshot.batches
    batch = workflow.runner.get_batch(ack.batch_uuid)
    assert batch_row.uuid == batch.uuid
    assert batch_row.status == batch.status.name
    assert batch_row.task_count == 1


def test_roster_hands_the_core_built_stubs_through_in_order(e2e_repo):
    workflow, session, client = session_client(e2e_repo)
    roster = client.roster()
    # asdict per row: TaskInfo equality compares only the key.
    assert [asdict(info) for info in roster] == [
        asdict(workflow.task_info(task))
        for task in workflow.get_filtered_tasks()
    ]
    # Stubs: no full-capture fields.
    assert all(info.attributes is None for info in roster)


def test_task_detail_serves_the_evaluated_full_capture(e2e_repo):
    workflow, session, client = session_client(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    info = client.task_detail(alpha.identity_key)
    assert info.key == alpha.identity_key
    assert info.attributes is not None
    assert info.source is not None
    assert info.effective_ttl == workflow.effective_check_ttl(alpha)
    with pytest.raises(RequestError):
        client.task_detail("no-such-key")


def test_history_and_record_detail_and_log_tail_match_the_record_store(e2e_repo):
    workflow, session, client = session_client(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    ack = run_to_completion(workflow, client, alpha)
    store = workflow.runner.record_store(ack.batch_uuid)
    record = store.get_record(alpha.identity_key)

    (row,) = client.history()
    assert row == HistoryRow.from_batch(
        workflow.runner.get_batch(ack.batch_uuid), store
    )
    assert row.uuid == ack.batch_uuid
    assert row.status == "FINISHED"
    outcome = row.tasks[alpha.identity_key]
    assert outcome.status == dict(store.items())[alpha.identity_key].name
    assert outcome.last_log == record.last_log

    detail = client.record_detail(ack.batch_uuid, alpha.identity_key)
    assert detail == RecordDetail.from_record(record)
    # The hand-through guarantee: the same TaskInfo the core built.
    assert detail.info is record.info
    assert [row.phase for row in detail.phases] == [
        span.phase.value for span in record.phases
    ]

    assert client.log_tail(ack.batch_uuid, alpha.identity_key) == record.log_tail(
        200
    )
    with pytest.raises(RequestError, match="keeps no records"):
        client.record_detail("no-such-batch", alpha.identity_key)
    with pytest.raises(RequestError, match="not in the roster"):
        client.log_tail(ack.batch_uuid, "no-such-key")


def test_caches_serve_the_inspection_projections(e2e_repo):
    workflow, session, client = session_client(e2e_repo, "my-cache")
    assert client.caches() == tuple(
        CacheCard.from_cache(cache) for cache in workflow.caches()
    )
    (weather,) = [card for card in client.caches() if card.name == "weather"]
    cache = workflow.get_cache("weather")
    assert weather.scope == "workflow"
    assert weather.info == tuple(cache.inspect())
    entry_names = {entry.name for entry in weather.entries}
    assert entry_names == {"cities", "city_index", "forecast"}
    # Eager entries are warm at collection time; forecast is lazy and cold.
    assert "cities" in weather.values
    assert "forecast" not in weather.values


def test_cache_value_renders_warm_and_reports_cold(e2e_repo):
    workflow, session, client = session_client(e2e_repo, "my-cache")
    view = client.cache_value("weather", "cities")
    assert view == CacheValueView.from_entry(workflow.get_cache("weather"), "cities")
    assert view.state == "warm"
    assert view.encoding == "text"
    assert "athens" in view.rendered

    cold = client.cache_value("weather", "forecast")
    assert cold.state == "cold"
    assert cold.rendered is None

    with pytest.raises(RequestError, match="names no cache"):
        client.cache_value("no-such-cache", "cities")
    with pytest.raises(RequestError, match="has no entry"):
        client.cache_value("weather", "no-such-entry")


def test_apply_filter_answers_keys_or_raises_the_parse_error(e2e_repo):
    workflow, session, client = session_client(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    keys = client.apply_filter("alpha")
    assert alpha.identity_key in keys
    with pytest.raises(RequestError):
        client.apply_filter("((unclosed")


def test_apply_filter_builtin_only_refuses_a_foreign_filter(e2e_repo, monkeypatch):
    from winslow.filter.builtin import GroupFilter

    monkeypatch.setattr("winslow.filter.builtin.BUILTIN_FILTERS", (GroupFilter,))
    workflow, session, client = session_client(e2e_repo)
    with pytest.raises(RequestError, match="supports only the builtin filters"):
        client.apply_filter("alpha", builtin_only=True)


def test_batch_options_and_session_params_mirror_the_workflow(e2e_repo):
    workflow, session, client = session_client(e2e_repo)
    assert client.batch_options() == asdict(workflow.batch_options)
    params = client.session_params()
    assert params == SessionParams.from_workflow(workflow)
    assert params.settings == workflow.settings_snapshot
    assert set(params.workflow_config) == set(workflow.config_option_names)


# --- the subscriptions ------------------------------------------------------------


def test_bus_topics_relay_the_published_payloads(e2e_repo):
    workflow, session, client = session_client(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    statuses, executions, created, completed = [], [], [], []
    client.subscribe(TaskStatusEvent, statuses.append)
    client.subscribe(ExecutionStatusEvent, executions.append)
    client.subscribe(BatchCreatedEvent, created.append)
    client.subscribe(BatchCompletedEvent, completed.append)

    ack = run_to_completion(workflow, client, alpha)
    wait_for(lambda: created, "no batch_created event")
    info = created[0].info
    assert isinstance(info, BatchInfo)
    assert info.uuid == ack.batch_uuid
    wait_for(lambda: completed, "no batch_completed event")
    assert completed[0].info.uuid == ack.batch_uuid
    assert completed[0].info.status == "FINISHED"
    wait_for(
        lambda: any(e.key == alpha.identity_key for e in statuses),
        "no task status event for alpha",
    )
    wait_for(
        lambda: any(
            e.task_key == alpha.identity_key and e.batch_uuid == ack.batch_uuid
            for e in executions
        ),
        "no execution status event for alpha",
    )

    client.unsubscribe(TaskStatusEvent, statuses.append)
    seen = len(statuses)
    run_to_completion(workflow, client, alpha)
    assert len(statuses) == seen


def test_subscribe_is_idempotent_and_unsubscribe_of_an_unknown_pair_is_a_noop(
    e2e_repo,
):
    workflow, session, client = session_client(e2e_repo)
    lines = []
    client.subscribe(SessionLogEvent, lines.append)
    client.subscribe(SessionLogEvent, lines.append)
    workflow.logger.warning("logged once")
    assert len(lines) == 1

    client.unsubscribe(SessionLogEvent, print)
    client.unsubscribe_task_log("no-such-key", print)


def test_session_log_lane_emits_the_logger_lines(e2e_repo):
    workflow, session, client = session_client(e2e_repo)
    lines = []
    client.subscribe(SessionLogEvent, lines.append)
    workflow.logger.warning("hello from the session logger")
    wait_for(
        lambda: any("hello from the session logger" in e.line for e in lines),
        "no session log event",
    )
    # A full log view carries the timestamped format: a timestamp prefixes
    # the level and the message (see INTERACTIVE_FORMATTER).
    line = next(e.line for e in lines if "hello from the session logger" in e.line)
    assert line.endswith("WARNING - hello from the session logger")
    assert line != "WARNING - hello from the session logger"
    client.unsubscribe(SessionLogEvent, lines.append)
    seen = len(lines)
    workflow.logger.warning("after the unsubscribe")
    assert len(lines) == seen


def test_cache_updated_lane_reports_the_cache_name(e2e_repo):
    workflow, session, client = session_client(e2e_repo, "my-cache")
    events = []
    client.subscribe(CacheUpdatedEvent, events.append)
    ack = client.submit(LoadCacheEntries(entries=(("weather", "forecast"),)))
    assert ack.accepted
    wait_for(
        lambda: any(e.cache_name == "weather" for e in events),
        "no cache_updated event for weather",
    )


def test_task_log_lane_serves_backlog_then_live_lines(e2e_repo, monkeypatch):
    workflow, session, client = session_client(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    original = type(alpha).run

    def run(self):
        self.logger.warning("alpha task-log hello")
        original(self)

    monkeypatch.setattr(type(alpha), "run", run)

    events, log_lines = [], []
    backlog = client.subscribe_task_log(alpha.identity_key, events.append)
    assert backlog == ()
    # The batch-scoped log lane fires for the same lines (see LogLineEvent).
    client.subscribe(LogLineEvent, log_lines.append)

    ack = run_to_completion(workflow, client, alpha)
    wait_for(
        lambda: any("alpha task-log hello" in e.line for e in events),
        "no task log event",
    )
    assert all(e.task_key == alpha.identity_key for e in events)
    wait_for(
        lambda: any(
            e.batch_uuid == ack.batch_uuid and "alpha task-log hello" in e.line
            for e in log_lines
        ),
        "no log line event",
    )

    client.unsubscribe_task_log(alpha.identity_key, events.append)
    seen = len(events)
    run_to_completion(workflow, client, alpha)
    assert len(events) == seen

    # A fresh subscribe now serves the buffered lines as its backlog, in the
    # same timestamped format as the live lane.
    late = []
    backlog = client.subscribe_task_log(alpha.identity_key, late.append)
    line = next(line for line in backlog if "alpha task-log hello" in line)
    assert line.endswith("WARNING - alpha task-log hello")
    assert line != "WARNING - alpha task-log hello"


def test_close_disconnects_every_subscription(e2e_repo):
    workflow, session, client = session_client(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    statuses, lines = [], []
    client.subscribe(TaskStatusEvent, statuses.append)
    client.subscribe(SessionLogEvent, lines.append)
    client.close()

    run_to_completion(workflow, client, alpha)
    workflow.logger.warning("after the close")
    assert statuses == []
    assert lines == []


# --- the actions --------------------------------------------------------------------


def test_submit_answers_the_acks_of_the_action_handler(e2e_repo):
    workflow, session, client = session_client(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    ack = client.submit(RunTasks(keys=(alpha.identity_key,)))
    assert isinstance(ack, BatchAck)
    assert ack.accepted
    workflow.runner.get_batch(ack.batch_uuid).wait()

    refused = client.submit(StopBatch(batch_uuid="no-such-batch"))
    assert refused.accepted is False
    assert "no-such-batch" in refused.reason


def test_check_tasks_submits_a_check_batch(e2e_repo):
    workflow, session, client = session_client(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    ack = client.submit(CheckTasks(keys=(alpha.identity_key,)))
    assert ack.accepted
    batch = workflow.runner.get_batch(ack.batch_uuid)
    batch.wait()
    assert batch.action.name == "CHECK"


def test_stop_batch_stops_a_running_batch(e2e_repo):
    workflow, tasks = gated_workflow(e2e_repo)
    client = LocalSessionClient(workflow.session)
    gate, batch = start_gated_batch(workflow, tasks)
    ack = client.submit(StopBatch(batch_uuid=batch.uuid))
    assert ack.accepted
    gate.set()
    batch.wait()
    assert batch.status is ExecutionStatus.STOPPED


def test_submit_options_snapshot_per_batch_through_the_port(e2e_repo):
    """The batch flags are per client, so they ride the submit: the batch
    snapshots them and the session baseline never changes."""
    workflow, session, client = session_client(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    ack = client.submit(
        RunTasks(keys=(alpha.identity_key,), options={"force_run": True})
    )
    assert ack.accepted, ack.reason
    workflow.runner.get_batch(ack.batch_uuid).wait()
    assert workflow.runner.get_batch(ack.batch_uuid).execution_context.force_run
    assert client.batch_options()["force_run"] is False

    refused = client.submit(
        RunTasks(keys=(alpha.identity_key,), options={"warp_speed": True})
    )
    assert not refused.accepted
    assert "names no batch option" in refused.reason


def test_clear_cache_entries_invalidates_through_the_handler(e2e_repo):
    workflow, session, client = session_client(e2e_repo, "my-cache")
    assert client.cache_value("weather", "cities").state == "warm"
    ack = client.submit(ClearCacheEntries(entries=(("weather", "cities"),)))
    assert ack.accepted
    # The ack means "started": the handler clears on a worker thread.
    wait_for(
        lambda: client.cache_value("weather", "cities").state == "cold",
        "cities never went cold",
    )


def test_cache_actions_run_under_the_session_log_scope(e2e_repo, state_store):
    """The action fan-out enters the session log scope, so a cache emission
    reaches the session log backlog (see ActionHandler._run_cache_entries)."""
    import logging

    registry = SessionRegistry()
    app = LocalAppClient(
        registry, orchestrator=local_orchestrator(e2e_repo), state_store=state_store
    )
    row = app.create_session("my-cache")
    client = app.session(row.session_id)
    # The drop line is info level; the run logger must let it through.
    registry.get(row.session_id).workflow.logger.setLevel(logging.INFO)

    ack = client.submit(ClearCacheEntries(entries=(("weather", "cities"),)))
    assert ack.accepted
    wait_for(
        lambda: any(
            "dropped" in line and "cities" in line
            for line in client.snapshot().session_log_backlog
        ),
        "the drop line never reached the session log",
    )


def test_end_session_publishes_session_ended_and_refuses_later_actions(e2e_repo):
    workflow, session, client = session_client(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    events = []
    client.subscribe(SessionEndedEvent, events.append)
    ack = client.submit(EndSession())
    assert ack.accepted
    wait_for(lambda: events, "no session_ended event")
    assert events[0].session_id == session.session_id
    assert session.has_ended

    refused = client.submit(RunTasks(keys=(alpha.identity_key,)))
    assert refused.accepted is False
    assert "has ended" in refused.reason


# --- the slice-three adapter guarantees ------------------------------------------


def test_roster_falls_back_past_a_bad_launch_filter(e2e_repo):
    workflow = build_workflow(
        e2e_repo, "my-workflow", Mode.TUI, "--filter", "((broken"
    )
    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    client = LocalSessionClient(session)
    with pytest.raises(MisconfigurationError):
        workflow.get_filtered_tasks()
    roster = client.roster()
    assert [info.key for info in roster] == [
        task.identity_key for task in workflow.tasks
    ]


def test_caches_isolate_an_unobservable_storage(e2e_repo):
    workflow, session, client = session_client(e2e_repo, "my-cache")
    broken = workflow.get_cache("weather")

    def raising_inspect():
        raise RuntimeError("disk gone")

    broken.inspect = raising_inspect
    cards = client.caches()
    (weather,) = [card for card in cards if card.name == "weather"]
    assert weather.error == "disk gone"
    assert weather.info == ()
    assert weather.values == {}
    # The declarations still stand, so a pane keeps its rows.
    assert {entry.name for entry in weather.entries} == {
        "cities",
        "city_index",
        "forecast",
    }
    # The other cards keep their observations.
    assert all(card.error is None for card in cards if card.name != "weather")


def test_snapshot_names_the_caches_until_the_session_ends(e2e_repo):
    """None separates "ended" from "no registered caches", which answers an
    empty tuple (see SessionSnapshot.cache_names)."""
    workflow, session, client = session_client(e2e_repo, "my-cache")
    assert "weather" in client.snapshot().cache_names
    client.submit(EndSession())
    assert client.snapshot().cache_names is None

    workflow, session, cacheless = session_client(e2e_repo)
    assert cacheless.snapshot().cache_names == ()


def test_cache_reads_refuse_an_ended_session_with_direction(e2e_repo):
    """The release guard names the true state: the session ended, and the
    recorded reads live in the history (see Workflow.caches)."""
    workflow, session, client = session_client(e2e_repo, "my-cache")
    client.submit(EndSession())
    assert session.has_ended
    with pytest.raises(RequestError, match="has ended and released its caches"):
        client.caches()
    with pytest.raises(RequestError, match="has ended and released its caches"):
        client.cache_value("weather", "cities")


def test_caches_before_initialize_tasks_keep_the_init_message(e2e_repo):
    from winslow.exceptions import InitializationError

    orchestrator = local_orchestrator(e2e_repo)
    workflow_kls = orchestrator.workflow_registry["my-cache"]
    workflow = workflow_kls(orchestrator.orchestrator_config)
    with pytest.raises(InitializationError, match="before initialize_tasks"):
        workflow.caches()


def test_history_rows_carry_the_batch_options(e2e_repo):
    workflow, session, client = session_client(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    ack = client.submit(
        RunTasks(keys=(alpha.identity_key,), options={"force_run": True})
    )
    assert ack.accepted, ack.reason
    workflow.runner.get_batch(ack.batch_uuid).wait()
    (row,) = client.history()
    assert row.options["force_run"] is True
    assert "batch_uuid" not in row.options


def test_descriptors_carry_auto_init_and_the_multiselect_selection(e2e_repo):
    from winslow.descriptors import ConfigOption
    from winslow.model import OptionRow

    app = LocalAppClient(SessionRegistry(), orchestrator=local_orchestrator(e2e_repo))
    descriptor = next(
        d for d in app.descriptors().workflows if d.workflow == "my-workflow"
    )
    assert descriptor.auto_init is False

    option = ConfigOption(type=int, multiselect=True, choices=[1, 2, 3])
    row = OptionRow.from_option("picks", option, current=[1, 3])
    assert row.initial == "1, 3"
    assert row.initial_selection == ("1", "3")


def test_validate_values_parses_strings_with_the_option_types():
    from types import SimpleNamespace

    from winslow.descriptors import ConfigOption
    from winslow.session import validate_values

    workflow_kls = SimpleNamespace(
        config_meta={
            "count": ConfigOption(type=int),
            "name": ConfigOption(),
            "picks": ConfigOption(type=int, multiselect=True, choices=[1, 2, 3]),
        }
    )
    orchestrator = SimpleNamespace(config_meta={"port": ConfigOption(type=int)})

    values, overrides = validate_values(
        "wf",
        workflow_kls,
        orchestrator,
        {"count": "7", "name": "x", "picks": ["1", "2"]},
        {"port": "9"},
    )
    assert values == {"count": 7, "name": "x", "picks": [1, 2]}
    assert overrides == {"port": 9}

    # A value that already has a type passes through unconverted.
    values, _ = validate_values("wf", workflow_kls, orchestrator, {"count": 7}, {})
    assert values == {"count": 7}

    with pytest.raises(ValueError, match="not a valid count"):
        validate_values("wf", workflow_kls, orchestrator, {"count": "seven"}, {})


def test_create_session_fills_unsent_options_from_the_cli_base(
    e2e_repo, state_store
):
    registry = SessionRegistry()
    app = LocalAppClient(
        registry,
        orchestrator=local_orchestrator(e2e_repo, "--client", "acme"),
        state_store=state_store,
    )
    # client is required; the parsed CLI base satisfies it without a value.
    row = app.create_session("my-identified")
    session = registry.get(row.session_id)
    assert session.workflow.workflow_config.client == "acme"

    # A sent value still wins over the base.
    row = app.create_session("my-identified", values={"client": "beta"})
    session = registry.get(row.session_id)
    assert session.workflow.workflow_config.client == "beta"


# --- the apply_filter scopes ------------------------------------------------------


def test_apply_filter_history_scope_matches_the_record_infos(e2e_repo):
    workflow, session, client = session_client(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    run_to_completion(workflow, client, alpha)
    assert client.apply_filter("alpha", scope="history") == (alpha.identity_key,)
    # A task with no execution record is not in the corpus.
    assert client.apply_filter("no-such-task", scope="history") == ()
    with pytest.raises(RequestError, match="names no filter scope"):
        client.apply_filter("alpha", scope="records")


def test_apply_filter_history_scope_survives_the_session_end(e2e_repo):
    workflow, session, client = session_client(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    run_to_completion(workflow, client, alpha)
    ack = client.submit(EndSession())
    assert ack.accepted
    wait_for(lambda: session.has_ended, "the session never ended")

    assert client.apply_filter("alpha", scope="history") == (alpha.identity_key,)
    with pytest.raises(RequestError, match="scope='history'"):
        client.apply_filter("alpha")


def test_apply_filter_history_scope_refuses_a_project_filter(e2e_repo, monkeypatch):
    from winslow.filter.builtin import GroupFilter

    monkeypatch.setattr("winslow.filter.builtin.BUILTIN_FILTERS", (GroupFilter,))
    workflow, session, client = session_client(e2e_repo)
    with pytest.raises(RequestError, match="supports only the builtin filters"):
        client.apply_filter("alpha", scope="history")


def test_apply_filter_history_scope_covers_a_task_outside_the_roster(e2e_repo):
    """A record survives its task leaving the roster: the history corpus is
    the record infos, not the launch-filtered task list."""
    workflow = build_workflow(
        e2e_repo, "my-workflow", Mode.TUI, "--filter", "no-such-task"
    )
    session = Session(workflow)
    workflow.check_pipeline_eligibility()
    client = LocalSessionClient(session)
    alpha = by_name(workflow)["Alpha"]
    assert alpha not in workflow.get_filtered_tasks()

    run_to_completion(workflow, client, alpha)
    assert client.apply_filter("alpha", scope="history") == (alpha.identity_key,)


def test_an_ending_session_finalizes_when_its_last_batch_drains(e2e_repo):
    """The drain rule lives in the runner: the last batch completion
    finalizes the end with no frontend in the loop (see
    HeadlessRunner._execute_batch)."""
    workflow, tasks = gated_workflow(e2e_repo)
    gate, batch = start_gated_batch(workflow, tasks)
    client = LocalSessionClient(workflow.session)
    events = []
    client.subscribe(SessionEndedEvent, events.append)

    ack = client.submit(EndSession())
    assert ack.accepted
    assert workflow.session.is_ending
    assert not workflow.session.has_ended

    gate.set()
    batch.wait()
    assert workflow.session.has_ended
    wait_for(lambda: events, "no session_ended event after the drain")


def test_restore_does_not_depend_on_the_argv_of_the_restoring_process(
    e2e_repo, state_store
):
    """The manifest stores the effective workflow values, CLI base included,
    so a process started without the original flags restores the session
    with them (see effective_workflow_values)."""
    registry = SessionRegistry()
    creating = LocalAppClient(
        registry,
        orchestrator=local_orchestrator(e2e_repo, "--client", "acme"),
        state_store=state_store,
    )
    row = creating.create_session("my-identified")
    manifest = state_store.load_manifest(row.session_id)
    assert manifest.workflow_values["client"] == "acme"

    # Simulate a dead process, then restore from one with a bare argv.
    registry.remove(row.session_id)
    restoring = LocalAppClient(
        SessionRegistry(),
        orchestrator=local_orchestrator(e2e_repo),
        state_store=state_store,
    )
    restored = restoring.restore_session(row.session_id)
    session = restoring.registry.get(restored.session_id)
    assert session.workflow.workflow_config.client == "acme"


def test_effective_workflow_values_round_trip_through_the_manifest():
    """Typed values survive the manifest: a JSON-native value stays as it
    is, everything else stores formatted and re-parses through the option
    type (see validate_values)."""
    from decimal import Decimal
    from types import SimpleNamespace

    from winslow.descriptors import ConfigOption
    from winslow.session import effective_workflow_values, validate_values

    meta = {
        "switch": ConfigOption(action="store_true"),
        "count": ConfigOption(type=int),
        "rate": ConfigOption(type=Decimal),
        "picks": ConfigOption(type=int, multiselect=True, choices=[1, 2, 3]),
        "unset": ConfigOption(),
    }
    workflow_kls = SimpleNamespace(config_meta=meta)
    base = SimpleNamespace(switch=True, count=7, rate=Decimal("1.5"))

    stored = effective_workflow_values(workflow_kls, base, {"picks": [1, 3]})
    assert stored == {
        "switch": True,
        "count": 7,
        "rate": "1.5",
        "picks": [1, 3],
    }

    values, _ = validate_values(
        "wf", workflow_kls, SimpleNamespace(config_meta={}), stored, {}
    )
    assert values == {
        "switch": True,
        "count": 7,
        "rate": Decimal("1.5"),
        "picks": [1, 3],
    }
