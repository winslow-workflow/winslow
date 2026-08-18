import hashlib
import json
import operator
from argparse import ArgumentParser, Namespace

from winslow._config import _ConfigBase
from winslow.cache import (
    WORKFLOW_SCOPE,
    CacheContainer,
    WorkflowCacheRegistry,
    initialize_global_cache,
    workflow_cache_context,
)
from winslow.filter import FilterRegistry
from winslow.task import TaskRegistry, TaskStatus
from winslow.task.context import BatchOptions
from winslow.task.status import PASSING_STATUSES, UNSUCCESSFUL_STATUSES
from winslow.constants import Mode
from winslow.runner.store import TaskStore, InteractiveStore
from winslow.graph import Graph
from winslow.runner import HeadlessRunner, InteractiveRunner
from winslow.session import Session
from winslow.logger import LOGGER
from winslow.util import slugify
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
        Mode.TUI: InteractiveStore,
    }

    runner_classes = {
        Mode.HEADLESS: HeadlessRunner,
        Mode.TUI: InteractiveRunner,
    }

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
        )
        self.filter_registry = self.filter_registry_class(
            orchestrator_config=orchestrator_config,
            workflow_config=self.workflow_config,
        )
        self.logger = logger
        # The Session does not exist at construction time. It is created after
        # the workflow init and attaches itself here.
        self._session = None

        # initialize_tasks builds the containers, before it builds the graph.
        self._workflow_cache = None
        self._global_cache = None

        self.batch_options = BatchOptions(
            dry_run=orchestrator_config.dry_run,
            force_run=orchestrator_config.force_run,
            force_success=orchestrator_config.force_success,
            disable_concurrency=orchestrator_config.disable_concurrency,
        )

        if store is None:
            self.logger.debug(f"Auto-initializing store for {self}")
            self.store = self.generate_store(
                orchestrator_config=orchestrator_config,
                workflow_config=self.workflow_config,
            )
        else:
            self.store = store
        self.runner = self.runner_classes[orchestrator_config.mode](
            orchestrator_config=orchestrator_config,
            workflow=self,
            store=self.store,
            logger=self.logger,
            workflow_config=self.workflow_config,
            batch_options=self.batch_options,
        )

    @classmethod
    def generate_store(cls, orchestrator_config, workflow_config):
        return cls.store_classes[orchestrator_config.mode]()

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
    def tasks(self):
        return sorted([t for t in self.store.keys()], key=operator.attrgetter("_index"))

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
        payload = json.dumps(
            [self.instance_name, self.identifiers_dict_safe], sort_keys=True
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:8]

    @property
    def cache_namespace(self):
        """The directory of the persistent cache tiers of this run (see
        JsonFileStorage): readable prefix plus digest, stable across sessions."""
        return f"{self.identity_prefix}-{self.identity_hash}"

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
            self.store[task] = TaskStatus.INITIALIZED

        logger.debug(f"{self} initialized {len(self.store)} tasks.")

        # The graph is necessary only to build the pipeline and to assign the
        # dependencies of each task, which the tasks now hold. Drop the graph, so
        # the garbage collector can free it and its _task_class_map, which holds
        # a reference to each task. The store and the task dependency links then
        # own the tasks.
        self.graph = None

    def release_tasks(self):
        # The single release point, at session end: history holds values and
        # uuids, so the store is the last owner of each task. Batch errors go
        # first, because their traceback frames reference tasks. The lock-free
        # clear is safe only because the session lifecycle guarantees that no
        # batch runs and that no batch can be admitted here.
        self.runner.release_batch_errors()
        self.store.clear()
        # The container dies with the session. Only a new session builds fresh
        # WorkflowCache instances.
        self._workflow_cache = None

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
            t for t, s in self.store.items() if s is TaskStatus.COMPLETED_WITH_ERROR
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
