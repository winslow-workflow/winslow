import operator
import time
import uuid
from argparse import ArgumentParser, Namespace
from functools import cached_property

from winslow._config import _ConfigBase
from winslow.cache import (
    WORKFLOW_SCOPE,
    CacheContainer,
    WorkflowCacheRegistry,
    initialize_global_cache,
    workflow_cache_context,
)
from winslow.filter import FilterRegistry
from winslow.filter.builtin import enforce_builtin_only
from winslow.task import TaskIndex, TaskRegistry, TaskStatus
from winslow.task.context import BatchOptions
from winslow.model import TaskInfo
from winslow.task.status import PASSING_STATUSES, UNSUCCESSFUL_STATUSES
from winslow.constants import Mode
from winslow.runner.store import TaskStore, log_task_status
from winslow.graph import Graph
from winslow.runner import HeadlessRunner, InteractiveRunner
from winslow.session import Session, SessionStatus
from winslow.bus import SessionBus
from winslow.events import Origin, SessionEndedEvent, TaskStatusEvent
from winslow.state import (
    SessionManifest,
    SessionPersistenceAdapter,
    StaleSweeper,
    is_trusted,
)
from winslow.logger import LOGGER
from winslow.util import identity_digest, slugify
from winslow.exceptions import MisconfigurationError, InitializationError


class Workflow(_ConfigBase):
    name = None

    # If True, the interactive app initializes this workflow at launch with the
    # default args. It does not show the selector, the form or the confirmation.
    # This is useful for a test. More than one workflow can set it, and the app
    # initializes each of them.
    auto_init = False

    store_classes = {
        Mode.HEADLESS: TaskStore,
        Mode.TUI: TaskStore,
    }

    runner_classes = {
        Mode.HEADLESS: HeadlessRunner,
        Mode.TUI: InteractiveRunner,
    }

    # The default check TTL of the tasks, in seconds: a passing check younger
    # than this counts as verified without a probe. None means always probe.
    # A task overrides it with its own check_ttl (see BaseRunner).
    check_ttl = None

    graph_class = Graph
    registry_class = TaskRegistry

    filter_registry_class = FilterRegistry

    cache_registry_class = WorkflowCacheRegistry

    def __init__(
        self, orchestrator_config, workflow_config=None, store=None, logger=LOGGER
    ):

        super().__init__(orchestrator_config)

        self.workflow_config = (
            workflow_config if workflow_config is not None else Namespace()
        )
        # The caches see only the workflow config, so the identity travels on
        # it (see WorkflowCache._storage_namespace). The name is safe: a config
        # option cannot bind it, because it clashes with the property.
        self.workflow_config.cache_namespace = self.cache_namespace
        self.registry = self.registry_class(
            orchestrator_config=orchestrator_config,
            workflow_config=self.workflow_config,
        )
        self.graph = self.graph_class(
            orchestrator_config=orchestrator_config,
            workflow_config=self.workflow_config,
            logger=logger,
        )
        self.filter_registry = self.filter_registry_class(
            orchestrator_config=orchestrator_config,
            workflow_config=self.workflow_config,
        )
        self.logger = logger
        # The Session does not exist at construction time. It is created after
        # the workflow init and attaches itself here.
        self._session = None
        # The workflow owns its persistence adapter and stale sweeper. None
        # until init_state attaches them; archive_state detaches them.
        self.persistence_listener = None
        self.stale_sweeper = None

        # initialize_tasks builds the containers, before it builds the graph.
        self._workflow_cache = None
        self._global_cache = None
        self._task_index = None
        # The workflow owns the prepared tasks; the task index holds weak
        # references. initialize fills the list, release_tasks clears it.
        self.tasks = None

        # The session baseline of the batch options, from the CLI. A submit can
        # carry its own values; this never changes (see BatchOptions).
        self.batch_options = BatchOptions(
            dry_run=orchestrator_config.dry_run,
            force_run=orchestrator_config.force_run,
            force_success=orchestrator_config.force_success,
            disable_concurrency=orchestrator_config.disable_concurrency,
        )

        if store is None:
            self.logger.debug(f"Auto-initializing store for {self}")
            self.bus = SessionBus()
            self.store = self.generate_store(
                bus=self.bus,
                orchestrator_config=orchestrator_config,
                workflow_config=self.workflow_config,
            )
        else:
            # A given store carries its bus, so the workflow adopts it: one
            # bus per session, however the store was built.
            self.store = store
            self.bus = store.bus
        if orchestrator_config.mode is Mode.HEADLESS:
            # The store publishes each transition; the log line is a
            # subscriber, and the mode decides who listens (see log_task_status).
            self.bus.subscribe(TaskStatusEvent, log_task_status)
        self.runner = self.runner_classes[orchestrator_config.mode](
            orchestrator_config=orchestrator_config,
            workflow=self,
            store=self.store,
            logger=self.logger,
            workflow_config=self.workflow_config,
            batch_options=self.batch_options,
        )

    @classmethod
    def generate_store(cls, bus, orchestrator_config, workflow_config):
        return cls.store_classes[orchestrator_config.mode](bus)

    @property
    def check(self):
        return self.orchestrator_config.check

    @property
    def dry_run(self):
        return self.batch_options.dry_run

    @property
    def force_run(self):
        return self.batch_options.force_run

    @property
    def force_success(self):
        return self.batch_options.force_success

    @property
    def disable_concurrency(self):
        return self.batch_options.disable_concurrency

    @property
    def session(self):
        return self._session

    @property
    def session_id(self):
        # A log record can be emitted before a session attaches, so "no session"
        # must be a legal state. The stamp filter accepts None.
        return self._session.session_id if self._session else None

    @property
    def identifiers_dict_safe(self):
        """{option name: display value} for the config options with
        identifier=True. identifier implies required, so each option always
        has a value and the code reads it directly."""
        return {
            name: option.format_value(getattr(self.workflow_config, name))
            for name, option in self.config_meta.items()
            if option.identifier
        }

    @property
    def identifier_suffix(self):
        """A key=value list of the identifier options. This part makes two
        runs of the same workflow different; empty without such options."""
        return " | ".join(f"{k}={v}" for k, v in self.identifiers_dict_safe.items())

    @property
    def identity_prefix(self):
        """A readable prefix: the instance name plus the scalar identifier
        values. Not unique - a structured value (a multiselect list, a tuple)
        is dropped, so pair it with identity_hash, which covers the full dict."""
        scalars = [
            formatted
            for name, formatted in self.identifiers_dict_safe.items()
            if isinstance(getattr(self.workflow_config, name), (str, int, float, bool))
        ]
        return slugify("-".join([self.instance_name, *scalars]))

    @property
    def identity_hash(self):
        """A digest of the full identity: two runs collide only when the name
        and every identifier match, however identity_prefix flattened them."""
        return identity_digest(self.instance_name, self.identifiers_dict_safe)

    @property
    def cache_namespace(self):
        """The directory of the persistent cache tiers of this run (see
        JsonFileStorage): readable prefix plus digest, stable across sessions."""
        return f"{self.identity_prefix}-{self.identity_hash}"

    @cached_property
    def run_nonce(self):
        """The nonce separates two concurrent runs of one workflow in the log
        routing (see Task.log_key). The property owns the name, so a config
        option cannot bind it."""
        return str(uuid.uuid4())

    def __str__(self):
        """The display form of the run: the name plus the identifier options,
        for example "etl (client=acme)" - the same shape as str(task)."""
        # _Base.__init__ logs through __str__ before workflow_config is set.
        if getattr(self, "workflow_config", None) is None:
            return self.instance_name
        if not self.identifier_suffix:
            return self.instance_name
        return f"{self.instance_name} ({self.identifier_suffix})"

    @classmethod
    def should_be_initialized(cls, orchestrator_config, parameters=None):
        return True

    @property
    def global_cache(self):
        if self._global_cache is None:
            raise InitializationError(
                f"{self} caches read before initialize_tasks built them."
            )
        return self._global_cache

    @property
    def workflow_cache(self):
        if self._workflow_cache is None:
            raise InitializationError(
                f"{self} caches read before initialize_tasks built them."
            )
        return self._workflow_cache

    @property
    def task_index(self):
        """Resolve an identity key to a live task (see TaskIndex)."""
        if self._task_index is None:
            raise InitializationError(
                f"{self} task index read before initialize_tasks built it."
            )
        return self._task_index

    def _initialize_caches(self):
        """Build and populate the containers of both scopes: the graph and the
        pre-graph hooks read them (see docs/caching.md)."""
        self._global_cache = initialize_global_cache(
            self.orchestrator_config,
            self.disable_concurrency,
            clear=self.orchestrator_config.clear_cache,
        )
        registry = self.cache_registry_class(self.orchestrator_config)
        registry.collect_classes(self.module_directory)
        instances = {
            kls.get_name(): kls(self.workflow_config) for kls in registry.classes
        }
        self._workflow_cache = CacheContainer(instances, scope=WORKFLOW_SCOPE)
        if self.orchestrator_config.clear_cache:
            # Before the population, so the eager loaders run fresh and a
            # persistent tier rewrites. A memory tier is cold anyway.
            self._workflow_cache.clear_all()
        self._workflow_cache.populate_eager_entries(self.disable_concurrency)

    def initialize_tasks(self, logger=LOGGER):
        if self.graph is None:
            raise InitializationError(
                f"{self} tasks already initialized - initialize_tasks is one-shot."
            )

        logger.debug(f"{self} initializing tasks.")

        self._initialize_caches()
        self.registry.collect_classes(self.module_directory)

        # The context serves the classmethod hooks that run before a task
        # instance exists (see winslow.cache.get_workflow_cache).
        with workflow_cache_context(self._workflow_cache):
            tasks = self.graph.generate_pipeline(self.registry)

        for task in tasks:
            # A batch thread reads the stamp, because the thread pool does not
            # propagate a context variable.
            task._workflow_cache_container = self._workflow_cache
            task._global_cache_container = self._global_cache
            # The nonce goes first: the buffer registers under log_key, and
            # the nonce prefixes that key.
            task._run_nonce = self.run_nonce
            if self.orchestrator_config.is_interactive:
                task._enable_log_buffer()
            self.store[task] = TaskStatus.INITIALIZED

        self.tasks = sorted(tasks, key=operator.attrgetter("_index"))
        self._task_index = TaskIndex(tasks)

        logger.debug(f"{self} initialized {len(self.store)} tasks.")

        # The graph is necessary only to build the pipeline and to assign the
        # dependencies of each task, which the tasks now hold. Drop the graph, so
        # the garbage collector can free it and its _task_class_map, which holds
        # a reference to each task. The workflow task list and the dependency
        # links then own the tasks.
        self.graph = None

    def release_tasks(self):
        # The single release point, at session end: history holds values and
        # uuids, so the workflow task list is the last owner of each task.
        # Batch errors go first, because their traceback frames reference tasks.
        self.runner.release_batch_errors()
        self.store.clear()
        self.tasks = None
        # The container dies with the session. Only a new session builds fresh
        # WorkflowCache instances.
        self._workflow_cache = None

    def effective_check_ttl(self, task):
        """The check TTL of the task: its own declaration when set, else the
        workflow default (see check_ttl)."""
        return task.check_ttl if task.check_ttl is not None else self.check_ttl

    def task_info(self, task, **kwargs):
        """The TaskInfo of the task, with the trust fields of the check_ttl
        rule filled from the stored snapshot (see TaskInfo.from_task)."""
        entry = self.load_snapshot(task.identity_key)
        return TaskInfo.from_task(
            task,
            checked_at=entry.checked_at if entry is not None else None,
            effective_ttl=self.effective_check_ttl(task),
            **kwargs,
        )

    def subscribe(self, event_type, handler):
        """Subscribe the handler to the session events of this workflow."""
        self.bus.subscribe(event_type, handler)

    def unsubscribe(self, event_type, handler):
        """Disconnect the handler (see subscribe). An unknown handler is a
        no-op, so a teardown path can run twice."""
        self.bus.unsubscribe(event_type, handler)

    def add_cache_listener(self, listener):
        """Attach the listener to the caches this workflow can see: the
        workflow cache and the global cache."""
        self.workflow_cache.add_listener(listener)
        self.global_cache.add_listener(listener)

    def remove_cache_listener(self, listener):
        """Detach the listener from both caches (see add_cache_listener). A
        no-op on the workflow cache once the session end has released it
        (see release_tasks): a teardown that races the session end must not
        raise."""
        if self._workflow_cache is not None:
            self._workflow_cache.remove_listener(listener)
        self.global_cache.remove_listener(listener)

    def caches(self):
        """The caches this workflow can see, workflow scope first."""
        return (*self.workflow_cache.caches(), *self.global_cache.caches())

    def get_cache(self, name):
        """The live cache named `name`, or None."""
        for cache in self.caches():
            if cache.get_name() == name:
                return cache
        return None

    def init_state(
        self,
        state_store,
        origin=None,
        orchestrator_overrides=None,
        workflow_values=None,
    ):
        """Start persistence for the attached session: the manifest, and the
        persistence listener on the store. Call this once the pipeline is
        runnable, after the eligibility pass: the manifest marks the session
        as a restore candidate. A failure degrades to a run without state."""
        if self.persistence_listener is not None:
            # A second registration doubles the writes.
            return
        session = self._session
        adapter = sweeper = None
        try:
            adapter = SessionPersistenceAdapter(state_store, session.session_id)
            adapter.attach(self)
            self.persistence_listener = adapter
            sweeper = StaleSweeper(self)
            self.bus.subscribe(TaskStatusEvent, sweeper.on_task_status)
            self.stale_sweeper = sweeper
            # The manifest lands last: a failure before this point leaves no
            # durable state.
            state_store.save_manifest(
                SessionManifest(
                    session_id=session.session_id,
                    workflow_class=type(self).get_name(),
                    workflow_namespace=self.cache_namespace,
                    orchestrator_overrides=orchestrator_overrides,
                    workflow_values=workflow_values,
                    origin=origin,
                    started_at=session.start,
                )
            )
        except Exception:
            # A persistence failure must not break the session start: the
            # session degrades to a run without state. The locals include a
            # subscriber whose registration failed.
            if adapter is not None:
                adapter.detach(self)
                adapter.close()
            if sweeper is not None:
                self.bus.unsubscribe(TaskStatusEvent, sweeper.on_task_status)
                sweeper.close()
            self.persistence_listener = None
            self.stale_sweeper = None
            self.logger.error(
                f"Could not persist the manifest of {session.session_id} - "
                f"the session runs without state",
                exc_info=True,
            )

    def load_snapshot(self, key):
        """The latest snapshot of the key, or None. The listener overlays the
        writes of this session on the initial state, so the read stays in
        memory (see SessionPersistenceAdapter.get)."""
        listener = self.persistence_listener
        return listener.get(key) if listener is not None else None

    def seed_from_state(self):
        """Feed the persisted state of the session onto the store: the
        snapshots replay as statuses, and the open batch records register as
        INTERRUPTED. Call this after the eligibility pass: that pass
        overwrites earlier status writes."""
        listener = self.persistence_listener
        if listener is None:
            return
        self._seed_task_statuses(listener.initial_state)
        self.runner.seed_interrupted_batches(listener.load_open_batches())

    def _seed_task_statuses(self, snapshots):
        """Replay the last terminal status of each task that the eligibility
        pass left READY_TO_PROCESS."""
        for task in self.tasks:
            if self.store[task] is not TaskStatus.READY_TO_PROCESS:
                continue
            entry = snapshots.get(task.identity_key)
            status = TaskStatus.__members__.get(entry.status) if entry else None
            if status is None:
                continue
            if status in PASSING_STATUSES and not is_trusted(
                entry.checked_at,
                self.effective_check_ttl(task),
                self._session.start,
                time.time(),
            ):
                # An untrusted success seeds as STALE: the next touch
                # re-verifies it (see TaskStatus.STALE).
                status = TaskStatus.STALE
            # An ordinary store write, so the subscribers see a normal
            # event. The SEED origin keeps checked_at where the probe
            # left it (see SessionPersistenceAdapter).
            self.runner.set_status(task, status, None, origin=Origin.SEED)

    def archive_state(self):
        """End persistence: stop the sweeper and the writer, unsubscribe them,
        then stamp and archive the manifest. After the durable writes the bus
        publishes SessionEndedEvent and closes, which disconnects every
        remaining subscriber. The session end calls this, and a persistence
        failure must not break the end. A session in ERROR archives as failed
        (see StateStore.mark_errored)."""
        if (sweeper := self.stale_sweeper) is not None:
            sweeper.close()
            self.bus.unsubscribe(TaskStatusEvent, sweeper.on_task_status)
            self.stale_sweeper = None
        if (listener := self.persistence_listener) is not None:
            listener.close()
            self.bus.unsubscribe(TaskStatusEvent, listener.on_task_status)
            self.persistence_listener = None
            try:
                if self.session.status is SessionStatus.ERROR:
                    listener.mark_errored()
                else:
                    listener.mark_ended()
            except Exception:
                self.logger.error(
                    f"Could not archive the manifest of {self.session_id}",
                    exc_info=True,
                )
        self.bus.publish(SessionEndedEvent(session_id=self.session_id))
        self.bus.close()

    def check_pipeline_eligibility(self, logger=LOGGER):
        tasks = self.tasks
        logger.debug(f"Checking eligibility for {len(tasks)} tasks.")
        self.runner.check_eligibility(tasks)

    @property
    def settings_snapshot(self):
        return {
            "dry_run": self.dry_run,
            "force_run": self.force_run,
            "force_success": self.force_success,
            "check": self.check,
            "disable_concurrency": self.disable_concurrency,
            "env": self.env,
        }

    @property
    def filter(self):
        return getattr(self.orchestrator_config, "filter", None)

    def get_filtered_tasks(self, query=None):
        tasks = self.tasks
        query = query or self.filter
        if not query:
            return tasks
        try:
            return self.filter_registry.parse(query).apply(tasks)
        except ValueError as e:
            # Report the bad filter and do not run everything silently. The parse
            # error message names the exact part that is wrong.
            raise MisconfigurationError(f"Invalid filter: {e}") from e

    def roster_tasks(self):
        """The tasks a roster read serves, in launch-filter order. A bad
        launch filter logs and answers every task, so an interactive client
        still renders the list (see get_filtered_tasks)."""
        try:
            return self.get_filtered_tasks()
        except MisconfigurationError:
            self.logger.error(
                "The launch filter does not parse - the roster lists every task.",
                exc_info=True,
            )
            return self.tasks

    def record_infos(self):
        """One TaskInfo per task with an execution record, across every
        batch. The record stores survive the session end, so a history
        search works after the task release (see ExecutionRecordStore)."""
        return tuple(
            {
                record.info.key: record.info
                for store in self.runner.record_stores()
                for record in store.records
            }.values()
        )

    def filter_keys(self, query, scope="tasks", builtin_only=False):
        """The identity keys the query matches over the named corpus: 'tasks'
        applies the full registry over the live tasks, 'history' the builtin
        filters over the record infos. Raises ValueError with direction."""
        if scope not in ("tasks", "history"):
            raise ValueError(
                f"{scope!r} names no filter scope - the scopes are "
                f"'tasks' and 'history'."
            )
        parsed = self.filter_registry.parse(query)
        if scope == "history":
            enforce_builtin_only(parsed)
            return tuple(info.key for info in parsed.apply(self.record_infos()))
        if builtin_only:
            enforce_builtin_only(parsed)
        if self.tasks is None:
            raise ValueError(
                f"{self} has ended and released its tasks - search the "
                f"execution records with scope='history'."
            )
        return tuple(task.identity_key for task in parsed.apply(self.tasks))

    def headless_run(self):
        # This looks unused, but the construction of the Session attaches it as
        # _session, and the runner needs it as its logging identity. The
        # interactive path gets its session from the UI, so a headless run must
        # make its own.
        Session(self)

        # Validate and resolve the filter first, so a bad --filter fails
        # immediately, before the eligibility pass runs and logs for each task.
        filtered_tasks = self.get_filtered_tasks()

        if self.filter and not filtered_tasks:
            raise MisconfigurationError(
                f"Filter {self.filter!r} matched no tasks in {self} - nothing to run."
            )

        self.runner.check_eligibility(self.tasks)

        if self.check:
            self.runner.check(filtered_tasks)
        else:
            self.runner.run(filtered_tasks)

        statuses = list(self.store.values())
        completed = sum(1 for s in statuses if s in PASSING_STATUSES)
        unsuccessful = sum(1 for s in statuses if s in UNSUCCESSFUL_STATUSES)

        self.logger.info(
            f"{self} finished - {completed} completed, "
            f"{unsuccessful} unsuccessful, {len(statuses)} total."
        )

        flagged = [
            key for key, s in self.store.items() if s is TaskStatus.COMPLETED_WITH_ERROR
        ]
        if flagged:
            self.logger.warning(
                f"{len(flagged)} task(s) completed despite errors during "
                f"processing - check task logs: {', '.join(map(str, flagged))}"
            )
        return unsuccessful == 0

    @classmethod
    def get_parser(cls, lenient=False):
        if not cls.config_meta:
            return None

        parser = ArgumentParser(
            prog=f"Workflow - {cls.get_name()}",
            description="Sub parser for a workflow",
        )

        for arg_ctx in cls.get_argparse_context(lenient=lenient):
            name = arg_ctx.pop("arg_name")
            parser.add_argument(name, **arg_ctx)

        return parser
