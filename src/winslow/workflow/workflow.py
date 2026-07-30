import operator
from argparse import ArgumentParser, Namespace

from winslow._config import _ConfigBase
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

    def __init__(
        self, orchestrator_config, workflow_config=None, store=None, logger=LOGGER
    ):

        super().__init__(orchestrator_config)

        self.workflow_config = (
            workflow_config if workflow_config is not None else Namespace()
        )
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
    def identifier_suffix(self):
        """A key=value list of the config options with identifier=True. This part
        makes two runs of the same workflow different. It is empty if the workflow
        declares no such option. identifier implies required, so each option always
        has a value and the code reads it directly."""
        parts = [
            f"{name}={option.format_value(getattr(self.workflow_config, name))}"
            for name, option in self.config_meta.items()
            if option.identifier
        ]
        return " | ".join(parts)

    @classmethod
    def should_be_initialized(cls, orchestrator_config, parameters=None):
        return True

    def initialize_tasks(self, logger=LOGGER):
        if self.graph is None:
            raise InitializationError(
                f"{self} tasks already initialized - initialize_tasks is one-shot."
            )

        logger.debug(f"{self} initializing tasks.")

        self.registry.collect_classes(self.module_directory)
        tasks = self.graph.generate_pipeline(self.registry)

        for task in tasks:
            self.store[task] = TaskStatus.INITIALIZED

        logger.debug(f"{self} initialized {len(self.store)} tasks.")

        # The graph is necessary only to build the pipeline and to assign the
        # dependencies of each task, which the tasks now hold. Drop the graph, so
        # the garbage collector can free it and its _task_class_map, which holds
        # a reference to each task. The store and the task dependency links then
        # own the tasks.
        self.graph = None

    def release_tasks(self):
        # Drop the references of the workflow to its tasks, so the garbage
        # collector can free each task that nothing else holds. A session calls
        # this when it ends. The store is then the only owner of each task,
        # because the graph is dropped after the init. A clear of the store thus
        # frees each task that an execution-history batch record does not keep,
        # directly or as a dependency of such a record. dict.clear does not take
        # the write lock of the store. This is safe only because the session
        # lifecycle guarantees that no batch runs and that no batch can be
        # admitted here.
        self.store.clear()

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
