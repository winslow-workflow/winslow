"""The in-process transport of the session port. LocalAppClient and
LocalSessionClient hand the dataclasses the core builds straight through:
no serialization runs on this path (see winslow.model)."""

from dataclasses import asdict
from functools import partial

from winslow.cache import declared_entries
from winslow.client.base import AppClient, SessionClient
from winslow.exceptions import MisconfigurationError
from winslow.logger import (
    INTERACTIVE_FORMATTER,
    InteractiveLogHandler,
    get_task_dispatcher,
)
from winslow.model import (
    CachesPayload,
    CacheUpdatedEvent,
    CacheValueView,
    Descriptors,
    HistoryRow,
    ManifestRow,
    RecordDetail,
    SessionLogEvent,
    SessionParams,
    SessionRow,
    SessionSnapshot,
    TaskLogEvent,
)
from winslow.session import create_session


class _CacheUpdateListener:
    """Collapse the five CacheListener callbacks into CacheUpdatedEvent per
    name. EventBridge does the same collapse on the serve side."""

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


class LocalAppClient(AppClient):
    """The dashboard scope over a live SessionRegistry. orchestrator and
    state_store power descriptors, manifests, create and restore; a client
    without them serves the registry reads only."""

    def __init__(self, registry, orchestrator=None, state_store=None):
        self.registry = registry
        self.orchestrator = orchestrator
        self.state_store = state_store

    def _require_orchestrator(self):
        if self.orchestrator is None:
            raise MisconfigurationError(
                "this client serves no workflows - pass an orchestrator."
            )

    def _require_state_store(self):
        if self.state_store is None:
            raise MisconfigurationError(
                "this client keeps no session state - pass a state store."
            )

    def sessions(self):
        return tuple(
            SessionRow.from_session(session)
            for session in self.registry.sessions()
        )

    def descriptors(self):
        self._require_orchestrator()
        return Descriptors.from_orchestrator(self.orchestrator)

    def manifests(self):
        self._require_state_store()
        return tuple(
            ManifestRow.from_manifest(manifest)
            for manifest in self.state_store.list_open_manifests()
            if manifest.session_id not in self.registry
        )

    def create_session(self, workflow, overrides=None, values=None):
        self._require_orchestrator()
        self._require_state_store()
        session = create_session(
            self.orchestrator,
            self.state_store,
            self.registry,
            workflow,
            overrides,
            values,
            origin="local",
        )
        return SessionRow.from_session(session)

    def restore_session(self, session_id):
        self._require_orchestrator()
        self._require_state_store()
        if session_id in self.registry:
            raise ValueError(f"{session_id!r} is already a live session.")
        manifest = next(
            (
                m
                for m in self.state_store.list_open_manifests()
                if m.session_id == session_id
            ),
            None,
        )
        if manifest is None:
            raise ValueError(
                f"{session_id!r} names no open manifest to restore."
            )
        if manifest.workflow_class not in self.orchestrator.workflow_registry.names:
            raise ValueError(
                f"the manifest names workflow {manifest.workflow_class!r}, "
                f"which this client does not collect."
            )
        session = create_session(
            self.orchestrator,
            self.state_store,
            self.registry,
            manifest.workflow_class,
            manifest.orchestrator_overrides or {},
            manifest.workflow_values or {},
            session_id=manifest.session_id,
            seed=True,
            origin="local",
        )
        return SessionRow.from_session(session)

    def session(self, session_id):
        return LocalSessionClient(self.registry.resolve(session_id))


class LocalSessionClient(SessionClient):
    """One live session, in-process. The reads build the same dataclasses
    the serve handlers serialize. A subscription handler runs on the thread
    that publishes, like a bus subscriber."""

    def __init__(self, session):
        self.session = session
        # One teardown callable per active subscription; close() drains it.
        self._teardowns = {}

    @property
    def session_id(self):
        return self.session.session_id

    @property
    def _workflow(self):
        return self.session.workflow

    # --- reads ---------------------------------------------------------------

    def snapshot(self):
        return SessionSnapshot.from_session(self.session)

    def roster(self):
        workflow = self._workflow
        return tuple(workflow.task_info(task) for task in workflow.roster_tasks())

    def task_detail(self, key):
        task = self._workflow.task_index.resolve(key)
        return self._workflow.task_info(
            task, full=True, evaluate=True, root_dir=self._workflow.root_dir
        )

    def record_detail(self, batch_uuid, key):
        return RecordDetail.from_record(self._record(batch_uuid, key))

    def history(self):
        runner = self._workflow.runner
        return tuple(
            HistoryRow.from_batch(batch, runner.record_store(batch.uuid))
            for batch in runner.batches
        )

    def log_tail(self, batch_uuid, key, limit=200):
        return self._record(batch_uuid, key).log_tail(limit)

    def _record(self, batch_uuid, key):
        store = self._workflow.runner.record_store(batch_uuid)
        if store is None:
            raise KeyError(
                f"batch {batch_uuid!r} keeps no records in this session."
            )
        try:
            return store.get_record(key)
        except KeyError:
            raise KeyError(
                f"task {key!r} is not in the roster of batch {batch_uuid!r}."
            ) from None

    def caches(self):
        return CachesPayload.from_workflow(self._workflow).caches

    def cache_value(self, cache_name, entry_name):
        cache = self._workflow.get_cache(cache_name)
        if cache is None:
            raise KeyError(f"{cache_name!r} names no cache of this session.")
        if entry_name not in declared_entries(type(cache)):
            raise KeyError(f"{cache} has no entry {entry_name!r}.")
        return CacheValueView.from_entry(cache, entry_name)

    def apply_filter(self, query, builtin_only=False, scope="tasks"):
        return self._workflow.filter_keys(
            query, scope=scope, builtin_only=builtin_only
        )

    def batch_options(self):
        return asdict(self._workflow.batch_options)

    def session_params(self):
        return SessionParams.from_workflow(self._workflow)

    # --- subscriptions ---------------------------------------------------------

    def subscribe(self, topic, handler):
        key = (topic, handler)
        if key in self._teardowns:
            return
        workflow = self._workflow
        if topic is CacheUpdatedEvent:
            listener = _CacheUpdateListener(handler)
            workflow.add_cache_listener(listener)
            teardown = partial(workflow.remove_cache_listener, listener)
        elif topic is SessionLogEvent:
            # The default formatter is INTERACTIVE: a full log view carries
            # timestamps; only an inline cell uses the short form.
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
        task = self._workflow.task_index.resolve(task_key)
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
        task = self._workflow.task_index.resolve(task_key)
        backlog = get_task_dispatcher().buffered(task.log_key)
        return tuple(INTERACTIVE_FORMATTER.format(record) for record in backlog)

    def unsubscribe_task_log(self, task_key, handler):
        teardown = self._teardowns.pop((TaskLogEvent, task_key, handler), None)
        if teardown is not None:
            teardown()

    def close(self):
        while self._teardowns:
            _, teardown = self._teardowns.popitem()
            teardown()

    # --- actions ----------------------------------------------------------------

    def submit(self, action):
        # submit, not submit_guarded: an in-process bug keeps its traceback
        # (see ActionHandler.submit_guarded).
        return self.session.actions.submit(action)
