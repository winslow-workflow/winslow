"""The in-process transport of the session port. LocalAppClient and
LocalSessionClient pass the dataclasses the core builds through unchanged
(see winslow.model)."""

import time
from dataclasses import asdict
from functools import partial

import psutil

from winslow import settings
from winslow.cache import declared_entries
from winslow.authz import audit, require, requires, visible
from winslow.client.base import AppClient, SessionClient
from winslow.exceptions import RequestError
from winslow.logger import (
    INTERACTIVE_FORMATTER,
    InteractiveLogHandler,
    get_task_dispatcher,
)
from winslow.permissions import LOCAL, Permission
from winslow.model import (
    CachesPayload,
    CacheUpdatedEvent,
    CacheValueView,
    Descriptors,
    BatchOutcome,
    EnvInfo,
    HostStats,
    ManifestInfo,
    RecordDetail,
    SessionLogEvent,
    SessionParams,
    SessionInfo,
    SessionSnapshot,
    TaskLogEvent,
)
from winslow.session import create_session


class CacheUpdateListener:
    """Collapse the five CacheListener callbacks into one CacheUpdatedEvent
    per name, handed to handler. The bridge and the local client share it."""

    def __init__(self, handler):
        self.handler = handler

    def on_entry_computed(self, info, previous_state):
        self.handler(CacheUpdatedEvent(cache_name=info.cache_name))

    def on_entries_invalidated(self, scope, dropped, trigger):
        for name in dropped:
            self.handler(CacheUpdatedEvent(cache_name=name))

    def on_eager_population_started(self, scope, entries):
        for name in entries:
            self.handler(CacheUpdatedEvent(cache_name=name))

    def on_eager_population_finished(self, scope, entries):
        for name in entries:
            self.handler(CacheUpdatedEvent(cache_name=name))

    def on_entry_error(self, scope, cache_name, entry_name, error):
        self.handler(CacheUpdatedEvent(cache_name=cache_name))


def _emit_session_log(handler, line):
    handler(SessionLogEvent(line=line))


def _emit_task_log(handler, task_key, line):
    handler(TaskLogEvent(task_key=task_key, line=line))


class HostStatsSampler:
    """A custom cpu percent over a window of `interval` seconds, shared by
    every client. psutil.cpu_percent keeps its previous counters in a module
    global, so several clients reading at once would need a lock."""

    def __init__(self, interval=1.0):
        self.interval = interval
        # (stamp, cpu counters at the stamp, stats of the window that ended there)
        self.window = (time.monotonic(), psutil.cpu_times(), None)

    def read(self):
        stamp, counters, stats = self.window
        now = time.monotonic()
        if stats is not None and now - stamp < self.interval:
            return stats
        fresh = psutil.cpu_times()
        total = sum(fresh) - sum(counters)
        idle = fresh.idle - counters.idle
        cpu_percent = round(100 * (total - idle) / total, 1) if total else 0.0
        stats = HostStats.sample(cpu_percent)
        # One tuple swap: a racing read computes the same window and wins nothing.
        self.window = (now, fresh, stats)
        return stats


class LocalAppClient(AppClient):
    """The dashboard scope over a live SessionRegistry. orchestrator and
    state_store serve descriptors, manifests, create and restore. A client
    without them serves the registry reads only."""

    def __init__(self, registry, orchestrator, state_store):
        self.registry = registry
        self.orchestrator = orchestrator
        self.state_store = state_store
        self.host_sampler = HostStatsSampler()

    @visible(Permission.VIEW_TASK)
    def sessions(self):
        return tuple(
            SessionInfo.from_session(session) for session in self.registry.sessions()
        )

    @visible(Permission.START_WORKFLOW, attr="workflows")
    def descriptors(self):
        return Descriptors.from_orchestrator(self.orchestrator)

    @visible(Permission.START_WORKFLOW)
    def manifests(self):
        return tuple(
            ManifestInfo.from_manifest(
                manifest, self._permission_scope(manifest.workflow_class)
            )
            for manifest in self.state_store.list_open_manifests()
            if manifest.session_id not in self.registry
        )

    def _permission_scope(self, workflow_name):
        registry = self.orchestrator.workflow_registry
        if workflow_name not in registry.names:
            return None
        return registry[workflow_name].get_permission_scope()

    def host_stats(self):
        return self.host_sampler.read()

    def environment(self):
        return EnvInfo(name=settings.env, color=settings.ENV_COLOR)

    def permissions(self):
        return LOCAL

    def create_session(self, workflow, overrides=None, values=None):
        # The scope name comes from the workflow argument, so the guard sits
        # here (see authz.BODY_GUARDED). An unknown name is checked as given, so
        # a caller without a scope learns no workflow name from the refusal.
        scope_name = self._permission_scope(workflow)
        require(
            Permission.START_WORKFLOW, scope_name or workflow, action="create_session"
        )
        try:
            session = create_session(
                self.orchestrator,
                self.state_store,
                self.registry,
                workflow,
                overrides,
                values,
                origin="local",
            )
        except (KeyError, ValueError) as exc:
            # A refusal: an unknown workflow, a bad option value. An init
            # bug propagates unchanged, so an in-process caller keeps its traceback.
            raise RequestError(exc.args[0] if exc.args else str(exc)) from None
        audit("create_session", scope_name, session.session_id, True)
        return SessionInfo.from_session(session)

    def restore_session(self, session_id):
        if session_id in self.registry:
            raise RequestError(f"{session_id!r} is already a live session.")
        manifest = next(
            (
                m
                for m in self.state_store.list_open_manifests()
                if m.session_id == session_id
            ),
            None,
        )
        if manifest is None:
            raise RequestError(f"{session_id!r} names no open manifest to restore.")
        if manifest.workflow_class not in self.orchestrator.workflow_registry.names:
            raise RequestError(
                f"the manifest names workflow {manifest.workflow_class!r}, "
                f"which this process does not collect."
            )
        scope_name = self._permission_scope(manifest.workflow_class)
        require(
            Permission.START_WORKFLOW,
            scope_name,
            action="restore_session",
            session_id=session_id,
        )
        session = create_session(
            self.orchestrator,
            self.state_store,
            self.registry,
            manifest.workflow_class,
            manifest.orchestrator_overrides or {},
            manifest.workflow_values or {},
            session_id=manifest.session_id,
            user=manifest.user,
            seed=True,
            origin="local",
        )
        audit("restore_session", scope_name, session.session_id, True)
        return SessionInfo.from_session(session)

    def session(self, session_id):
        return LocalSessionClient(self.registry.resolve(session_id))

    def subscribe_connection(self, handler):
        # The in-process transport has no connection to lose.
        pass


class LocalSessionClient(SessionClient):
    """One live session, in-process. The reads build the same dataclasses
    the serve handlers serialize. A subscription handler runs on the thread
    that publishes, like a bus subscriber."""

    def __init__(self, session):
        self.session = session
        # One teardown callable per active subscription. close() drains it.
        self._teardowns = {}

    @property
    def session_id(self):
        return self.session.session_id

    @property
    def _workflow(self):
        return self.session.workflow

    @property
    def permission_scope(self):
        return self.session.permission_scope

    # --- reads ---------------------------------------------------------------

    @requires(Permission.VIEW_TASK)
    def snapshot(self):
        return SessionSnapshot.from_session(self.session)

    def _require_live(self):
        """The live task reads refuse once the session has ended, with the
        reason the serve edge serves (see Connection.handle_subscribe_task_log)."""
        if self.session.has_ended:
            raise RequestError(
                f"{self.session_id} has ended - its live task and cache "
                f"state is released."
            )

    @requires(Permission.VIEW_TASK)
    def roster(self):
        self._require_live()
        workflow = self._workflow
        return tuple(workflow.task_info(task) for task in workflow.roster_tasks())

    @requires(Permission.VIEW_TASK)
    def task_detail(self, task_key):
        self._require_live()
        task = self._resolve_task(task_key)
        return self._workflow.task_info(
            task, full=True, evaluate=True, root_dir=self._workflow.root_dir
        )

    def _resolve_task(self, task_key):
        try:
            return self._workflow.task_index.resolve(task_key)
        except KeyError as exc:
            raise RequestError(exc.args[0]) from None

    @requires(Permission.VIEW_TASK)
    def record_detail(self, batch_uuid, task_key):
        return RecordDetail.from_record(self._record(batch_uuid, task_key))

    @requires(Permission.VIEW_TASK)
    def history(self):
        runner = self._workflow.runner
        return tuple(
            BatchOutcome.from_batch(batch, runner.record_store(batch.uuid))
            for batch in runner.batches
        )

    @requires(Permission.VIEW_TASK)
    def log_tail(self, batch_uuid, task_key, limit=None):
        # None arrives from an omitted wire field. The default lives here.
        return self._record(batch_uuid, task_key).log_tail(
            200 if limit is None else limit
        )

    def _record(self, batch_uuid, task_key):
        store = self._workflow.runner.record_store(batch_uuid)
        if store is None:
            raise RequestError(
                f"batch {batch_uuid!r} keeps no records in this session."
            )
        try:
            return store.get_record(task_key)
        except KeyError:
            raise RequestError(
                f"task {task_key!r} is not in the roster of batch {batch_uuid!r}."
            ) from None

    @requires(Permission.VIEW_TASK)
    def caches(self):
        # Workflow.caches raises for an ended session. Its message becomes
        # the refusal reason.
        try:
            return CachesPayload.from_workflow(self._workflow).caches
        except ValueError as exc:
            raise RequestError(str(exc)) from None

    @requires(Permission.VIEW_TASK)
    def cache_value(self, cache_name, entry_name):
        try:
            cache = self._workflow.get_cache(cache_name)
        except ValueError as exc:
            raise RequestError(str(exc)) from None
        if cache is None:
            raise RequestError(f"{cache_name!r} names no cache of this session.")
        if entry_name not in declared_entries(type(cache)):
            raise RequestError(f"{cache} has no entry {entry_name!r}.")
        return CacheValueView.from_entry(cache, entry_name)

    @requires(Permission.VIEW_TASK)
    def apply_filter(self, query, scope="tasks"):
        try:
            return self._workflow.filter_keys(query, scope=scope)
        except ValueError as exc:
            raise RequestError(str(exc)) from None

    @requires(Permission.VIEW_TASK)
    def batch_options(self):
        return asdict(self._workflow.batch_options)

    @requires(Permission.VIEW_TASK)
    def session_params(self):
        return SessionParams.from_workflow(self._workflow)

    # --- subscriptions ---------------------------------------------------------

    def subscribe(self, topic, handler):
        key = (topic, handler)
        if key in self._teardowns:
            return
        workflow = self._workflow
        if topic is CacheUpdatedEvent:
            listener = CacheUpdateListener(handler)
            workflow.add_cache_listener(listener)
            teardown = partial(workflow.remove_cache_listener, listener)
        elif topic is SessionLogEvent:
            # The default formatter is INTERACTIVE: a full log view carries
            # timestamps. Only an inline cell uses the short form.
            log_handler = InteractiveLogHandler(partial(_emit_session_log, handler))
            workflow.logger.addHandler(log_handler)
            teardown = partial(workflow.logger.removeHandler, log_handler)
        else:
            workflow.subscribe(topic, handler)
            teardown = partial(workflow.unsubscribe, topic, handler)
        self._teardowns[key] = teardown

    def unsubscribe(self, topic, handler):
        teardown = self._teardowns.pop((topic, handler), None)
        if teardown is not None:
            teardown()

    def subscribe_task_log(self, task_key, handler):
        key = (TaskLogEvent, task_key, handler)
        if key in self._teardowns:
            return self._backlog(task_key)
        task = self._resolve_task(task_key)
        log_handler = InteractiveLogHandler(partial(_emit_task_log, handler, task_key))
        dispatcher = get_task_dispatcher()
        # The listener attaches before the backlog read, so a line in that
        # window duplicates rather than disappears.
        dispatcher.add_listener(task.log_key, log_handler)
        self._teardowns[key] = partial(
            dispatcher.remove_listener, task.log_key, log_handler
        )
        return self._backlog(task_key)

    def _backlog(self, task_key):
        task = self._resolve_task(task_key)
        backlog = get_task_dispatcher().buffered(task.log_key)
        return tuple(INTERACTIVE_FORMATTER.format(record) for record in backlog)

    def unsubscribe_task_log(self, task_key, handler):
        teardown = self._teardowns.pop((TaskLogEvent, task_key, handler), None)
        if teardown is not None:
            teardown()

    def close(self):
        """Disconnect every subscription of this client (see EventBridge.detach)."""
        while self._teardowns:
            _, teardown = self._teardowns.popitem()
            teardown()

    # --- actions ----------------------------------------------------------------

    def submit(self, action):
        # The unguarded submit keeps the traceback of an in-process bug
        # (see ActionHandler.submit_guarded).
        return self.session.actions.submit(action)
