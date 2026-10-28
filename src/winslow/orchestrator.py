import os
import sys
import copy
import argparse
import importlib
from enum import Enum

from winslow.cache import (
    GlobalCacheRegistry,
    set_global_cache_registry,
    stray_workflow_caches,
)
from winslow.workflow import WorkflowRegistry
from winslow._config import _ConfigBase
from winslow.constants import DEFAULT_ENDPOINTS, ENDPOINTS, Mode, SHUTDOWN_GRACE
from winslow.descriptors import ConfigOption
from winslow.util import iter_dir_module_names, classes_in_module

from winslow.exceptions import (
    MisconfigurationError,
    InitializationError,
    EligibilityError,
)
from winslow.conf import import_string, settings
from winslow.state import belongs_to_env
from winslow.logger import (
    LOGGER,
    setup_run_logging,
    shutdown_run_logging,
    stdout_json_sink,
)
from winslow.telemetry import (
    activate_telemetry_configurations,
    emit_unscoped_error,
    shutdown_telemetry_configurations,
)


INDENT = "\t"


class Command(Enum):
    RUN = "run"
    SHOW = "show"
    SERVE = "serve"
    CONNECT = "connect"
    AUTH = "auth"
    SETTINGS = "settings"


def _parse_mode(value):
    """The argparse type for --mode. It converts a string into a Mode and gives a
    lowercase error message."""
    try:
        return Mode(value)
    except ValueError:
        choices = ", ".join(m.value for m in Mode)
        raise argparse.ArgumentTypeError(
            f"invalid choice: {value!r} (choose from {choices})"
        )


class OrchestratorConfig(argparse.Namespace):
    """The parsed CLI config, with the derived accessors. action and mode have
    class defaults, so is_interactive resolves on a subcommand that does not
    declare them. SHOW, for example, has no mode, and argparse then leaves the
    attribute unset."""

    action = Command.RUN
    mode = Mode.TUI

    @property
    def is_interactive(self):
        # This gates the interactive setup that SHOW does not need, especially
        # the log buffer of each task (see Graph). The store and the runner read
        # the mode instead.
        return self.action is not Command.SHOW and self.mode is not Mode.HEADLESS


def _merged_config(base, overrides):
    """Make a deep copy and do not change the base. The parsed base is used again
    in a later session and to pre-fill a form. A change in place would thus copy
    the inputs of one run into the next run. `base` is None for a workflow with
    no config."""
    merged = copy.deepcopy(base) if base is not None else argparse.Namespace()
    vars(merged).update(overrides)
    return merged


class Orchestrator(_ConfigBase):
    workflow_registry_class = WorkflowRegistry
    global_cache_registry_class = GlobalCacheRegistry

    workflow = ConfigOption(
        help_text="Name of the workflow to view / run.",
        subcommands=(
            Command.RUN.value,
            Command.SHOW.value,
        ),
        # The UI has a workflow selection widget, so it needs no text input.
        show_on_ui=False,
    )

    host = ConfigOption(
        help_text="The bind address of the serve process. Loopback needs no credential.",
        default="127.0.0.1",
        subcommands=Command.SERVE.value,
        show_on_ui=False,
    )

    port = ConfigOption(
        help_text="The port of the serve process.",
        type=int,
        default=8866,
        subcommands=Command.SERVE.value,
        show_on_ui=False,
    )

    endpoints = ConfigOption(
        help_text=(
            "The endpoints to serve: ws (the websocket), web (the browser UI at /, "
            "needs ws), mcp (requires the mcp extra)."
        ),
        choices=list(ENDPOINTS),
        multiselect=True,
        default=list(DEFAULT_ENDPOINTS),
        subcommands=Command.SERVE.value,
        show_on_ui=False,
    )

    filter = ConfigOption(
        help_text=(
            "Filter expression for tasks. Supports name matching (bare text), "
            "filter commands (!g <group>, !group <group>), "
            "boolean operators (& |), negation (~), grouping (()), "
            "and comma-separated OR shorthand (foo,bar)."
        ),
        subcommands=(
            Command.RUN.value,
            Command.SHOW.value,
        ),
        depends_on="initialize",
        show_on_ui=False,
    )

    initialize = ConfigOption(
        action="store_true",
        default=False,
        help_text=(
            "Initialize a single workflow (chosen with --workflow) and list its "
            "tasks. Required in order to use --filter with show."
        ),
        subcommands=Command.SHOW.value,
        show_on_ui=False,
    )

    with_deps = ConfigOption(
        action="store_true",
        default=False,
        help_text=(
            "With --initialize, also list each task's dependencies, in the order "
            "they run."
        ),
        subcommands=Command.SHOW.value,
        depends_on="initialize",
        show_on_ui=False,
    )

    mode = ConfigOption(
        type=_parse_mode,
        choices=tuple(Mode),
        help_text=(
            "How to run the workflow(s): 'tui' launches the interactive terminal"
            " UI; 'headless' runs single-thread/single-process with no UI, useful"
            " for CI and debugging."
        ),
        default=Mode.TUI,
        subcommands=Command.RUN.value,
        show_on_ui=False,
    )

    check = ConfigOption(
        action="store_true",
        help_text=(
            "Checks the tasks in the workflow instead of running them"
            " - available in headless run mode."
        ),
        default=False,
        subcommands=Command.RUN.value,
        show_on_ui=False,
    )

    disable_concurrency = ConfigOption(
        action="store_true",
        help_text=(
            "Disables all concurrency during task runs (e.g. task dependencies and task "
            "eligibility will be checked sequentially)."
        ),
        default=False,
        subcommands=Command.RUN.value,
        # Off the start form: the Tasks pane sets it per batch.
        show_on_ui=False,
    )

    clear_cache = ConfigOption(
        action="store_true",
        help_text=(
            "Invalidate every cache entry at workflow initialization, before "
            "the eager population."
        ),
        default=False,
        subcommands=Command.RUN.value,
    )

    dry_run = ConfigOption(
        action="store_true",
        help_text=(
            "Run tasks in dry-run mode (dry_run method will be "
            "called on tasks, instead of run)"
        ),
        default=False,
        subcommands=Command.RUN.value,
        # Off the start form: the Tasks pane sets it per batch.
        show_on_ui=False,
    )

    force_run = ConfigOption(
        action="store_true",
        help_text=(
            "Skips implicit success checks before a task run. "
            "Runnability and dependency checks will still be in effect."
        ),
        default=False,
        subcommands=Command.RUN.value,
        # Off the start form: the Tasks pane sets it per batch.
        show_on_ui=False,
    )

    force_success = ConfigOption(
        action="store_true",
        help_text=(
            "Mark tasks as successful (FORCE_SUCCESS) without running any checks "
            "or the task itself. Ineligible (skipped) tasks are left untouched."
        ),
        default=False,
        subcommands=Command.RUN.value,
        # Off the start form: the Tasks pane sets it per batch.
        show_on_ui=False,
    )

    reraise_errors = ConfigOption(
        action="store_true",
        help_text=(
            "Re-raise unexpected task errors after marking the task ERROR, "
            "aborting the run instead of continuing. For CI and debugging."
        ),
        default=False,
        subcommands=Command.RUN.value,
        show_on_ui=False,
    )

    debug = ConfigOption(
        action="store_true",
        help_text="Enable debug mode and logging.",
        default=False,
        subcommands=(
            Command.RUN.value,
            Command.SHOW.value,
        ),
        # The command line enables the debug mode.
        show_on_ui=False,
    )

    def __init__(self, orchestrator_config, directory=None, unknown_args=None):
        self.directory = directory or os.getcwd()
        # argparse reads sys.argv if this is None. Normalize it at the boundary.
        self.unknown_args = list(unknown_args or ())

        super().__init__(orchestrator_config)

        self.workflow_registry = self.workflow_registry_class(self.orchestrator_config)
        self.global_cache_registry = self.global_cache_registry_class(
            self.orchestrator_config
        )

        # The TUI application. run and connect create it.

        self.logger = LOGGER

    @classmethod
    def _add_arguments(cls, parser, subcommand=None):
        for arg_ctx in cls.get_argparse_context(subcommand):
            try:
                name = arg_ctx.pop("arg_name")
                parser.add_argument(name, **arg_ctx)
            except (TypeError, KeyError):
                cmd = subcommand if subcommand else "base"
                LOGGER.error(
                    f"Could not add argument for {cmd} parser {parser}, context: '{name}' - {arg_ctx}"
                )
                raise

    @classmethod
    def _generate_subcommand(
        cls,
        subparsers,
        action,
        help_text,
    ):
        sub_parser = subparsers.add_parser(action.value, help=help_text)

        cls._add_arguments(sub_parser, subcommand=action.value)
        sub_parser.set_defaults(action=action)

        return sub_parser

    @classmethod
    def get_base_parser(cls):

        parser = argparse.ArgumentParser(
            prog="Winslow",
            description="A state and workflow management framework with a terminal UI",
        )

        cls._add_arguments(parser)

        subparsers = parser.add_subparsers(help="Available subcommands")

        all_defaults = {
            name: conf.default
            for name, conf in cls.config_meta.items()
            if conf.subcommands
        }
        parser.set_defaults(**all_defaults)

        cls._generate_subcommand(
            subparsers,
            action=Command.SHOW,
            help_text="Show workflow / task information.",
        )

        cls._generate_subcommand(
            subparsers, action=Command.RUN, help_text="Run workflow(s)."
        )

        cls._generate_subcommand(
            subparsers,
            action=Command.SERVE,
            help_text="Serve the live sessions over one websocket endpoint.",
        )

        connect_parser = cls._generate_subcommand(
            subparsers,
            action=Command.CONNECT,
            help_text="Run the TUI against a serve process.",
        )
        # The ConfigOption machinery declares only --flag options, so the
        # URL is added here as the one positional argument of the CLI.
        connect_parser.add_argument(
            "url",
            help="The websocket URL of the serve process, e.g. ws://host:8866. "
            "A non-loopback server needs a credential: `winslow auth login` "
            "stores one for the URL.",
        )

        from winslow.access import cli

        cli.add_parser(subparsers).set_defaults(action=Command.AUTH)
        subparsers.add_parser(
            "settings",
            help="Print every setting, its source and what each path resolves to.",
        ).set_defaults(action=Command.SETTINGS)

        return parser

    @classmethod
    def parse_args(cls):
        base_parser = cls.get_base_parser()
        base_args, unknown_args = base_parser.parse_known_args(
            namespace=OrchestratorConfig()
        )
        return base_args, unknown_args

    @property
    def sorted_workflow_classes(self):
        return sorted(
            self.workflow_registry.classes,
            key=lambda kls: (kls.get_module_path(), kls.__name__),
        )

    @classmethod
    def get_from_cwd(cls):
        klasses = []
        for scoped_name in iter_dir_module_names(os.getcwd(), recursive=False):
            try:
                module = importlib.import_module(scoped_name)
            # Catch SystemExit too. A sys.exit() in an unguarded script must not
            # stop the CLI.
            except (Exception, SystemExit) as e:
                # The orchestrator is optional. A broken file that has no
                # relation to it must not stop the CLI.
                LOGGER.warning(
                    f"Skipping {scoped_name.split('.', 1)[1]}.py - "
                    f"{type(e).__name__}: {e}",
                    exc_info=True,
                )
                continue
            klasses.extend(classes_in_module(module, cls))

        if len(klasses) > 1:
            names = ", ".join(kls.__name__ for kls in klasses)

            raise MisconfigurationError(
                f"Multiple concrete orchestrator classes found in {os.getcwd()}: {names}. "
                f"Please define one orchestrator at most per directory."
            )

        return klasses[0] if klasses else None

    def _parse_known_workflow_args(self, workflow_kls, unknown):
        """Returns the parsed args, or None, and the tokens that this workflow did
        not claim."""
        parser = workflow_kls.get_parser(lenient=True)
        if parser is None:
            return None, tuple(unknown)
        try:
            # Lenient: the args of another workflow are not for this parser.
            args, leftover = parser.parse_known_args(unknown)
        except SystemExit:
            # argparse already printed the message. Send the exit to the clean
            # error path.
            raise MisconfigurationError(
                f"Invalid arguments for workflow '{workflow_kls.get_name()}' (see above)."
            )
        return args, tuple(leftover)

    @classmethod
    def _leftover_indices(cls, unknown, leftover):
        """The positions in `unknown` that argparse did not claim for this
        workflow. The match is by position, because two workflows can claim
        equal tokens: `--my-integer 7 --batch-size 7` leaves a `7` in each
        leftover list, and a match by value would report that `7` as unclaimed."""
        positions = set()
        cursor = 0
        for i, token in enumerate(unknown):
            if cursor < len(leftover) and token == leftover[cursor]:
                positions.add(i)
                cursor += 1
        return positions

    def collect_workflow_args(self):
        """{workflow_kls: parsed args or None}, validated together.

        `show`, the interactive start, and a serve process's descriptors
        request call this. They parse the args of each discovered workflow
        at one time, so a start form (local or remote) prefills from the
        same CLI-supplied values. A headless run has one named workflow and
        parses its args strictly (see `_handle_headless_run`), so the
        collision between workflows that this method handles cannot occur
        there."""
        parsed = {
            kls: self._parse_known_workflow_args(kls, self.unknown_args)
            for kls in self.sorted_workflow_classes
        }

        # A token is unrecognized if no workflow claimed its position. A position
        # that one workflow claimed leaves the intersection.
        unclaimed = set(range(len(self.unknown_args)))
        for _, leftover in parsed.values():
            unclaimed &= self._leftover_indices(self.unknown_args, leftover)

        if unclaimed:
            tokens = [self.unknown_args[i] for i in sorted(unclaimed)]
            raise MisconfigurationError(f"Unrecognized arguments: {' '.join(tokens)}")

        return {kls: args for kls, (args, _) in parsed.items()}

    def _display_workflow(self, idx, workflow, show_tasks):
        self.logger.info(
            f"{idx + 1}. {workflow.__class__.__name__} ({workflow.instance_name})"
            f" - {workflow.module_directory}"
        )

        if show_tasks:
            self._display_tasks(workflow)
        else:
            self._display_task_classes(workflow)

    def _display_task_classes(self, workflow):
        for i, kls in enumerate(
            sorted(workflow.registry.classes, key=lambda k: k.get_name())
        ):
            parameterized = getattr(kls, "_is_parameterized", False)
            marker = "  [parameterized]" if parameterized else ""
            self.logger.info(f"{INDENT}{i + 1}. {kls.__name__}{marker}")

            if parameterized:
                for declared_name, param in kls._parameterization_meta.items():
                    self.logger.info(
                        f"{INDENT * 2}- {self._describe_parameter(kls, declared_name, param)}"
                    )

    @classmethod
    def _describe_parameter(cls, kls, declared_name, param):
        style = param.param_style.name.lower()
        if param._compound:
            names = ", ".join(kls._parameter_attrs[declared_name])
            return f"{declared_name} -> {names}  ({style})"
        return f"{declared_name}  ({style})"

    def _display_tasks(self, workflow):
        for task in workflow.get_filtered_tasks():
            self.logger.info(f"{INDENT}{task._index + 1}. {str(task)}")

            if self.orchestrator_config.with_deps and task.dependent_tasks:
                self.logger.info(f"{INDENT * 2}Dependencies:")
                for dep in sorted(task.dependent_tasks, key=lambda d: d._index):
                    self.logger.info(f"{INDENT * 3}{dep._index + 1}. {str(dep)}")

    def check_filters(self, workflow):
        wanted = getattr(self.orchestrator_config, "workflow", None)
        return not wanted or workflow.get_name() == wanted

    def _handle_show(self):
        if self.orchestrator_config.initialize:
            return self._handle_initialize()
        self._list_workflow_classes()

    def _list_workflow_classes(self):
        self.logger.debug("Listing workflow classes")
        workflow_args_map = self.collect_workflow_args()
        workflows = []

        for workflow_kls in self.sorted_workflow_classes:
            if not workflow_kls.should_be_initialized(self.orchestrator_config):
                continue

            workflow = workflow_kls(
                self.orchestrator_config,
                workflow_args_map[workflow_kls],
                root_dir=self.directory,
            )

            if not self.check_filters(workflow):
                continue

            workflow.registry.collect_classes(workflow.module_directory)
            workflows.append(workflow)

        for idx, workflow in enumerate(workflows):
            self._display_workflow(idx, workflow, show_tasks=False)

    def _handle_initialize(self):
        self.logger.debug("Initializing a single workflow")
        workflow_name = self.orchestrator_config.workflow

        if not workflow_name:
            raise MisconfigurationError(
                "--initialize requires a single workflow - pass --workflow <name>."
            )

        if workflow_name not in self.workflow_registry:
            raise MisconfigurationError(
                f"{workflow_name} not found in the workflow registry"
                f" - the available workflows are {self.workflow_registry.names}."
            )

        workflow_kls = self.workflow_registry[workflow_name]
        workflow_args = self._parse_workflow_args(workflow_kls)

        if not workflow_kls.should_be_initialized(self.orchestrator_config):
            raise InitializationError(
                f"Cannot initialize workflow {workflow_kls} - its should_be_initialized answered False for this configuration."
            )

        workflow = workflow_kls(
            self.orchestrator_config, workflow_args, root_dir=self.directory
        )
        workflow.initialize_tasks()
        self._display_workflow(0, workflow, show_tasks=True)

    def _parse_workflow_args(self, workflow_kls):
        """Parse strictly for the one workflow that is selected to run. `required`
        and depends_on are thus enforced here. The lenient collective parse, which
        only lists the workflows, does not enforce them."""
        parser = workflow_kls.get_parser()
        if parser is None:
            if self.unknown_args:
                raise MisconfigurationError(
                    f"Unrecognized arguments: {' '.join(self.unknown_args)}"
                )
            return None
        args = parser.parse_args(self.unknown_args)
        workflow_kls.validate_option_dependencies(args)
        return args

    def _handle_run(self):
        if self.orchestrator_config.mode is Mode.HEADLESS:
            return self._handle_headless_run()
        self._handle_interactive_run()

    def _handle_headless_run(self):
        self.logger.debug("Headless run")

        workflow_name = self.orchestrator_config.workflow

        if not workflow_name:
            raise MisconfigurationError(
                f"a headless run initializes one workflow - pass --workflow "
                f"<name>; the collected workflows are "
                f"{self.workflow_registry.names}."
            )

        if workflow_name not in self.workflow_registry:
            raise MisconfigurationError(
                f"{workflow_name} not found in the workflow registry"
                f" - the available workflows are {self.workflow_registry.names}."
            )

        workflow_kls = self.workflow_registry[workflow_name]
        workflow_args = self._parse_workflow_args(workflow_kls)

        if not workflow_kls.should_be_initialized(self.orchestrator_config):
            error = InitializationError(
                f"Cannot initialize workflow {workflow_kls} - its should_be_initialized answered False for this configuration."
            )
            emit_unscoped_error(
                error, workflow_name=workflow_name, workflow_class=workflow_kls.__name__
            )
            raise error

        workflow = workflow_kls(
            self.orchestrator_config, workflow_args, root_dir=self.directory
        )

        # A run that does not start is invisible under cron, so the telemetry
        # hook must report it. The catch is narrow on purpose:
        # MisconfigurationError is bad input, and a reraise_errors escape is
        # already reported at the task boundary.
        try:
            workflow.initialize_tasks()
            return workflow.headless_run()
        except (EligibilityError, InitializationError) as e:
            emit_unscoped_error(
                e,
                workflow_name=workflow.instance_name,
                workflow_instance=str(workflow),
                workflow_class=type(workflow).__name__,
                session_id=workflow.session_id,
            )
            raise

    def _handle_serve(self):
        try:
            from winslow.serve.app import ServeApp
            from winslow.serve.server import HttpServer
        except ImportError as e:
            raise MisconfigurationError(
                "Serve mode requires the serve extra - install with: "
                "pip install 'winslow[serve]'"
            ) from e
        from winslow.serve.app import serve_origins
        from winslow.serve.auth import Authenticator
        from winslow.session import SessionRegistry

        config = self.orchestrator_config
        self.logger.info(f"Serving on {config.host}:{config.port}")
        authenticator = Authenticator.from_settings(config.host)
        for level, line in authenticator.describe(f"{config.host}:{config.port}"):
            self.logger.log(level, line)
        # The boundary must exist before the first session logs (see
        # setup_run_logging). WINSLOW_LOG_JSON sends the run lane to stdout
        # for the log store of a pod; the default keeps the session files.
        setup_run_logging(sinks=[stdout_json_sink()] if settings.LOG_JSON else None)
        registry = SessionRegistry()
        state_store = import_string(settings.STATE_STORE)(config)
        self._restore_sessions(registry, state_store)
        self._auto_init_sessions(registry, state_store)
        serve = ServeApp(
            registry,
            authenticator,
            orchestrator=self,
            state_store=state_store,
            endpoints=config.endpoints,
            base_url=f"http://{config.host}:{config.port}",
            allowed_origins=serve_origins(config.host, config.port, config.endpoints),
        )
        try:
            # log_config=None: uvicorn's loggers propagate to the root
            # handler, so its lines share the winslow format - JSON under
            # WINSLOW_LOG_JSON (see _console_handler).
            HttpServer(
                serve,
                host=config.host,
                port=config.port,
                log_config=None,
                timeout_graceful_shutdown=SHUTDOWN_GRACE,
            ).run()
        finally:
            # A forced exit skips the lifespan, so the drain runs here for both paths.
            serve.drain(SHUTDOWN_GRACE)
            shutdown_run_logging()

    def _with_telemetry(self, handle):
        """Run handle with the configurations TELEMETRY lists active. A show
        produces no errors worth a backend, so it stays outside. The finally
        flushes and unregisters, so an embedding process can start again."""
        active = activate_telemetry_configurations(
            [import_string(path) for path in settings.TELEMETRY],
            self.orchestrator_config,
        )
        try:
            return handle()
        finally:
            shutdown_telemetry_configurations(active)

    def _restore_sessions(self, registry, state_store):
        """Rebuild every open manifest at serve startup: the sessions of a
        dead process come back without a client, the way a local user's
        restore brings them back. A failed rebuild logs and skips."""
        from winslow.client import LocalAppClient

        port = LocalAppClient(registry, orchestrator=self, state_store=state_store)
        foreign = [
            m
            for m in state_store.list_open_manifests()
            if not belongs_to_env(m, settings.ENV)
        ]
        for manifest in foreign:
            self.logger.warning(
                f"restore: leaving out {manifest.session_id}, a session of the "
                f"{manifest.env} environment - this process runs {settings.ENV}."
            )
        for manifest in port.manifests():
            self.logger.info(f"restore: rebuilding {manifest.session_id}")
            try:
                port.restore_session(manifest.session_id)
            except Exception:
                self.logger.error(
                    f"restore: the rebuild of '{manifest.session_id}' failed.",
                    exc_info=True,
                )

    def _auto_init_sessions(self, registry, state_store):
        """One session per auto_init workflow: the process that owns the
        sessions runs auto_init, so a connecting client starts none. A
        failed initialization logs and skips; the process serves on."""
        from winslow.session import create_session

        # A restored session satisfies auto_init: the workflow already runs.
        live = {session.workflow.instance_name for session in registry.sessions()}
        for name in self.workflow_registry.names:
            workflow_kls = self.workflow_registry[name]
            if not workflow_kls.auto_init or name in live:
                continue
            if not workflow_kls.should_be_initialized(self.orchestrator_config):
                continue
            self.logger.info(f"auto_init: initializing {name}")
            try:
                create_session(self, state_store, registry, name)
            except Exception:
                self.logger.error(
                    f"auto_init: the initialization of '{name}' failed.",
                    exc_info=True,
                )

    def _handle_auth(self):
        from winslow.access import cli

        try:
            from winslow.access.credentials import CredentialFile
        except ImportError as e:
            raise MisconfigurationError(
                "The auth commands require the connect or the serve extra - "
                "install with: pip install 'winslow[connect]'"
            ) from e

        # False exits with 1 and no further message, the way a failed run does
        # (see cli.main): the command already printed its answer.
        return cli.run(self.orchestrator_config, CredentialFile(), print) == 0

    def _handle_connect(self):
        """The remote TUI: the same app over the wire transport of the
        session port. The serve process owns the workflows and the state."""
        try:
            from winslow.client.websocket import RemoteAppClient
            from winslow.ui import Winslow
        except ImportError as e:
            raise MisconfigurationError(
                "Connect mode requires the connect extra - install with: "
                "pip install 'winslow[connect]'"
            ) from e

        from winslow.access.backends import StoredCredential
        from winslow.access.credentials import CredentialFile, remember_user

        config = self.orchestrator_config
        store = CredentialFile()
        client = RemoteAppClient(
            config.url, credential=StoredCredential(store, config.url)
        )
        client.connect()
        self.logger.info(f"Connected to {config.url}")
        remember_user(store, config.url, client.wire.hello)

        # The tasks run on the serve process, so no run record reaches this
        # one: run-log wiring here would only create an empty log directory.
        app = Winslow(client=client, logger=self.logger, owns_sessions=False)
        try:
            app.run()
        finally:
            client.close()

    def _handle_interactive_run(self):
        self.logger.debug("Interactive run")

        try:
            from winslow.ui import Winslow
        except ImportError as e:
            raise MisconfigurationError(
                "Interactive mode requires the UI extra - install with: pip install 'winslow[tui]'"
            ) from e

        # Validate the CLI args before any effect, so a typo fails immediately.
        # The descriptors read of the app parses them again per request.
        self.collect_workflow_args()

        # Set up the winslow.runs sink and the propagate=False boundary BEFORE a
        # workflow logger or a task logger starts to propagate. The run logs then
        # go to the sink and not to the console. This is interactive only. A
        # headless run keeps the console output.
        setup_run_logging()

        from winslow.client import LocalAppClient
        from winslow.client.local import process_user
        from winslow.session import SessionRegistry

        # The composition root of the local TUI: this process owns the registry
        # and the durable store (see winslow.state); the app consumes the port.
        local_client = LocalAppClient(
            SessionRegistry(),
            orchestrator=self,
            state_store=import_string(settings.STATE_STORE)(self.orchestrator_config),
            host_user=process_user(),
        )
        app = Winslow(client=local_client, logger=self.logger, owns_sessions=True)

        # app.run() blocks until the TUI stops. Then flush and stop the
        # run-logging listener. This is in a finally clause, so it also occurs
        # after an error. If it does not occur, the listener thread and the open
        # file handles stay, and the buffered lines are lost.
        try:
            app.run()
        finally:
            shutdown_run_logging()

    def initialize_workflow(
        self,
        workflow_kls,
        orchestrator_overrides,
        workflow_values,
        workflow_base=None,
        task_store=None,
        logger=LOGGER,
    ):
        # Put the UI values on the parsed base and do not build a new config from
        # them. The base holds the default of each declared option, and also of
        # an option with show_on_ui=False. The form path thus cannot drop an
        # option that it did not show.
        orchestrator_config = _merged_config(
            self.orchestrator_config, orchestrator_overrides
        )
        workflow_params = _merged_config(workflow_base, workflow_values)

        return workflow_kls(
            orchestrator_config,
            workflow_params,
            store=task_store,
            logger=logger,
            root_dir=self.directory,
        )

    def _collect_caches(self):
        """Collect the GlobalCache classes for the workflow initializations. A
        WorkflowCache outside every workflow directory only gets a warning."""
        self.global_cache_registry.collect_classes(self.directory)
        set_global_cache_registry(self.global_cache_registry)

        workflow_directories = [
            os.path.dirname(os.path.abspath(sys.modules[kls.__module__].__file__))
            for kls in self.workflow_registry.classes
        ]
        for kls in stray_workflow_caches(self.directory, workflow_directories):
            LOGGER.warning(
                f"WorkflowCache {kls.__name__} is outside every workflow "
                f"directory - no workflow collects it."
            )

    def start(self):
        """
        Do the action that self.orchestrator_config (base_args) and unknown_args
        select:

        - Start the UI.
        - Start a simple run.
        - Show a summary of the workflows and the tasks.
        """
        if self.orchestrator_config.action is Command.AUTH:
            # The stores alone: no workflow, no server.
            return self._handle_auth()
        if self.orchestrator_config.action is Command.SETTINGS:
            from winslow.conf.report import lines

            for line in lines(settings):
                print(line)
            return True

        self.validate_option_dependencies(
            self.orchestrator_config, subcommand=self.orchestrator_config.action.value
        )

        if self.orchestrator_config.action is Command.CONNECT:
            # A remote TUI reads everything over the wire, so the local
            # workflow and cache collection is skipped.
            return self._handle_connect()

        self.workflow_registry.collect_classes(self.directory)
        self._collect_caches()

        if self.orchestrator_config.action is Command.SHOW:
            self._handle_show()
        elif self.orchestrator_config.action is Command.SERVE:
            self._with_telemetry(self._handle_serve)
        elif self.orchestrator_config.action is Command.RUN:
            return self._with_telemetry(self._handle_run)
