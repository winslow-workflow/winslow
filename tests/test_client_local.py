"""The session port, slice two: the local adapter hands core-built
dataclasses through. Each read answers the same DTO the core state implies,
the subscriptions relay the bus payloads, and the app scope creates and
restores sessions through the shared create_session flow."""

import time

import pytest

from winslow.actions import BatchAck, LoadCacheEntries, RunTasks, StopBatch
from winslow.client import LocalAppClient, LocalSessionClient
from winslow.constants import Mode
from winslow.events import BatchCreatedEvent, TaskStatusEvent
from winslow.exceptions import MisconfigurationError
from winslow.model import (
    BatchInfo,
    CacheUpdatedEvent,
    SessionLogEvent,
    TaskLogEvent,
)
from winslow.orchestrator import Orchestrator, OrchestratorConfig
from winslow.session import Session, SessionRegistry

from harness import build_workflow, by_name


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


def local_orchestrator(e2e_repo):
    config, unknown = Orchestrator.get_base_parser().parse_known_args(
        ["serve"], namespace=OrchestratorConfig()
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
    with pytest.raises(KeyError, match="names no collected workflow"):
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
    with pytest.raises(ValueError, match="already a live session"):
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
    with pytest.raises(ValueError, match="names no open manifest"):
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
    assert [info.key for info in roster] == [
        task.identity_key for task in workflow.get_filtered_tasks()
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
    with pytest.raises(KeyError):
        client.task_detail("no-such-key")


def test_history_and_record_detail_and_log_tail_match_the_record_store(e2e_repo):
    workflow, session, client = session_client(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    ack = run_to_completion(workflow, client, alpha)
    store = workflow.runner.record_store(ack.batch_uuid)
    record = store.get_record(alpha.identity_key)

    (row,) = client.history()
    assert row.uuid == ack.batch_uuid
    assert row.status == "FINISHED"
    outcome = row.tasks[alpha.identity_key]
    assert outcome.status == dict(store.items())[alpha.identity_key].name
    assert outcome.last_log == record.last_log

    detail = client.record_detail(ack.batch_uuid, alpha.identity_key)
    # The hand-through guarantee: the same TaskInfo the core built.
    assert detail.info is record.info
    assert [row.phase for row in detail.phases] == [
        span.phase.value for span in record.phases
    ]

    assert client.log_tail(ack.batch_uuid, alpha.identity_key) == record.log_tail(
        200
    )
    with pytest.raises(KeyError, match="keeps no records"):
        client.record_detail("no-such-batch", alpha.identity_key)
    with pytest.raises(KeyError, match="not in the roster"):
        client.log_tail(ack.batch_uuid, "no-such-key")


def test_caches_serve_the_inspection_projections(e2e_repo):
    workflow, session, client = session_client(e2e_repo, "my-cache")
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
    assert view.state == "warm"
    assert view.encoding == "text"
    assert "athens" in view.rendered

    cold = client.cache_value("weather", "forecast")
    assert cold.state == "cold"
    assert cold.rendered is None

    with pytest.raises(KeyError, match="names no cache"):
        client.cache_value("no-such-cache", "cities")
    with pytest.raises(KeyError, match="has no entry"):
        client.cache_value("weather", "no-such-entry")


def test_apply_filter_answers_keys_or_raises_the_parse_error(e2e_repo):
    workflow, session, client = session_client(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    keys = client.apply_filter("alpha")
    assert alpha.identity_key in keys
    with pytest.raises(ValueError):
        client.apply_filter("((unclosed")


def test_apply_filter_builtin_only_refuses_a_foreign_filter(e2e_repo, monkeypatch):
    from winslow.filter.builtin import GroupFilter

    monkeypatch.setattr("winslow.filter.builtin.BUILTIN_FILTERS", (GroupFilter,))
    workflow, session, client = session_client(e2e_repo)
    with pytest.raises(ValueError, match="supports only the builtin filters"):
        client.apply_filter("alpha", builtin_only=True)


def test_batch_options_and_session_params_mirror_the_workflow(e2e_repo):
    from dataclasses import asdict

    workflow, session, client = session_client(e2e_repo)
    assert client.batch_options() == asdict(workflow.batch_options)
    params = client.session_params()
    assert params.settings == workflow.settings_snapshot
    assert set(params.workflow_config) == set(workflow.config_option_names)


# --- the subscriptions ------------------------------------------------------------


def test_bus_topics_relay_the_published_payloads(e2e_repo):
    workflow, session, client = session_client(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    statuses, batches = [], []
    client.subscribe(TaskStatusEvent, statuses.append)
    client.subscribe(BatchCreatedEvent, batches.append)

    ack = run_to_completion(workflow, client, alpha)
    wait_for(lambda: batches, "no batch_created event")
    info = batches[0].info
    assert isinstance(info, BatchInfo)
    assert info.uuid == ack.batch_uuid
    wait_for(
        lambda: any(e.key == alpha.identity_key for e in statuses),
        "no task status event for alpha",
    )

    client.unsubscribe(TaskStatusEvent, statuses.append)
    seen = len(statuses)
    run_to_completion(workflow, client, alpha)
    assert len(statuses) == seen


def test_session_log_lane_emits_the_logger_lines(e2e_repo):
    workflow, session, client = session_client(e2e_repo)
    lines = []
    client.subscribe(SessionLogEvent, lines.append)
    workflow.logger.warning("hello from the session logger")
    wait_for(
        lambda: any("hello from the session logger" in e.line for e in lines),
        "no session log event",
    )
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

    events = []
    backlog = client.subscribe_task_log(alpha.identity_key, events.append)
    assert backlog == ()

    run_to_completion(workflow, client, alpha)
    wait_for(
        lambda: any("alpha task-log hello" in e.line for e in events),
        "no task log event",
    )
    assert all(e.task_key == alpha.identity_key for e in events)

    client.unsubscribe_task_log(alpha.identity_key, events.append)
    seen = len(events)
    run_to_completion(workflow, client, alpha)
    assert len(events) == seen


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
