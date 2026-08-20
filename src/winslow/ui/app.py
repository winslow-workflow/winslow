import asyncio
import logging
import traceback

from textual import on
from textual.app import App, ScreenStackError
from textual.widgets import Footer, Header, Button

from winslow.ui import screens
from winslow.ui.store_adapter import (
    SessionLifecycleAdapter,
    SessionLifecycleEvent,
    TuiCacheAdapter,
    TuiStoreAdapter,
)
from winslow.actions import EndSession
from winslow.events import (
    BatchCompletedEvent,
    BatchCreatedEvent,
    ExecutionStatusEvent,
    LogLineEvent,
    TaskStatusEvent,
)
from winslow.session import Session
from winslow.state import create_state_store
from winslow.util import generate_id
from winslow.task.context import LogContext, scoped_log_context
from winslow.logger import run_logger_name


class Winslow(App):
    BINDINGS = [
        ("ctrl+d", "switch_mode('dashboard')", "Dashboard"),
    ]

    MODES = {
        "dashboard": screens.DashboardScreen,
    }

    DEFAULT_MODE = "dashboard"

    CSS_PATH = [
        "styles/common.tcss",
        "styles/dashboard.tcss",
        "styles/workflow.tcss",
        "styles/modals.tcss",
    ]

    def __init__(self, orchestrator, orchestrator_config, workflow_context):
        # A private name, to prevent a clash if Textual adds a config object.
        self._winslow_config = orchestrator_config
        self.workflow_context = workflow_context
        self.orchestrator = orchestrator  # necessary to generate the workflows

        self.session_store = {}
        # The cache adapter per session id: the end of the session must detach
        # it from the global container, which outlives every session.
        self._cache_adapters = {}
        # One durable store for every session of the app: manifests, task
        # snapshots and batch records live here (see winslow.state).
        self.state_store = create_state_store(orchestrator_config)

        super().__init__()

    def clear_selection(self):
        try:
            super().clear_selection()
        except ScreenStackError:
            pass

    @property
    def logger(self):
        return self.orchestrator.logger

    def compose(self):
        """Create the child widgets of the app."""
        yield Header()  # todo color coding on env and annotate debug mode
        yield Footer()

    @property
    def workflow_name_map(self):
        # key: workflow name, value: workflow kls
        return {kls.get_name(): kls for kls in self.workflow_context.keys()}

    @property
    def dashboard(self):
        # The dashboard is a MODE. Its base screen is not in the registry of the
        # named screens that get_screen reads, so this property reads the stack
        # of the mode directly.
        return self._screen_stacks["dashboard"][0]

    async def _create_workflow(self, workflow_kls, form_values):
        await self._start_session(
            workflow_kls,
            orchestrator_overrides=form_values.orchestrator,
            workflow_values=form_values.workflow,
        )

    async def _restore_session(self, manifest):
        """Rebuild one session from its manifest and seed it from its snapshots.
        The dashboard offers this for every open manifest at app start."""
        workflow_kls = self.workflow_name_map.get(manifest.workflow_class)
        if workflow_kls is None:
            await self.dashboard.add_failed_session(
                manifest.workflow_class,
                f"The manifest of {manifest.session_id} names the workflow "
                f"{manifest.workflow_class!r}, which this repository does not "
                f"declare. Restore needs the workflow class.",
            )
            self.notify(
                f"Unknown workflow '{manifest.workflow_class}'",
                title="Restore failed",
                severity="error",
            )
            return
        await self._start_session(
            workflow_kls,
            orchestrator_overrides=manifest.orchestrator_overrides or {},
            workflow_values=manifest.workflow_values or {},
            session_id=manifest.session_id,
            seed=True,
        )

    async def _start_session(
        self,
        workflow_kls,
        orchestrator_overrides,
        workflow_values,
        session_id=None,
        seed=False,
    ):
        workflow_name = workflow_kls.get_name()
        self.logger.debug(
            f"Workflow start message: {workflow_name} - params: {workflow_values}"
        )

        # Generate the session id first, so the same id names the workflow
        # logger, identifies the Session and stamps each log line. session_id
        # needs only the workflow name, which is available before the workflow
        # exists. A restore passes the id of the manifest instead.
        session_id = session_id or generate_id(workflow_name)

        row = await self.dashboard.add_pending_session(workflow_name)

        self.logger.info(f"Initializing workflow: {workflow_name}")

        # The logger is under winslow.runs.*, so the init logs and the workflow
        # logs reach the run sink. session_id makes it unique per session, so the
        # handler of the workflow log pane receives only its own records.
        # propagate is True, because winslow.runs is the stop boundary.
        workflow_logger = logging.getLogger(run_logger_name(session_id))
        workflow_logger.propagate = True

        # The log context at run level: it stamps the logs of the init, the
        # task generation and the eligibility, which run outside a task_scope.
        # The workflow instance is not built yet, so the name stands in for it.
        init_log_ctx = LogContext(
            session_id=session_id,
            workflow_name=workflow_name,
            workflow_instance=workflow_name,
            task_name=None,
            task_instance=None,
            batch_uuid=None,
        )
        with scoped_log_context(init_log_ctx):
            try:
                workflow = await asyncio.to_thread(
                    self.orchestrator.initialize_workflow,
                    workflow_kls=workflow_kls,
                    orchestrator_overrides=orchestrator_overrides,
                    workflow_values=workflow_values,
                    workflow_base=self.workflow_context.get(workflow_kls),
                    logger=workflow_logger,
                )

                self.logger.info(f"Workflow '{workflow_name}' initialized.")
                session = Session(workflow, session_id=session_id)
                self.session_store[session.session_id] = session

                self.logger.info(f"Initializing tasks: {workflow_name}")

                await asyncio.to_thread(
                    workflow.initialize_tasks,
                    logger=workflow.logger,
                )

                await asyncio.to_thread(
                    workflow.check_pipeline_eligibility,
                    logger=workflow.logger,
                )

                # Persistence starts only once the pipeline is runnable: a kill
                # during the initialization above leaves no restore candidate.
                workflow.init_state(
                    self.state_store,
                    origin="tui",
                    orchestrator_overrides=orchestrator_overrides,
                    workflow_values=workflow_values,
                )

                if seed:
                    # After the eligibility pass (see Workflow.seed_from_state).
                    await asyncio.to_thread(workflow.seed_from_state)

                self.logger.info(f"Workflow '{workflow_name}' ready.")
            except Exception as e:
                tb = traceback.format_exc()
                # The full traceback goes to the session log, which is the
                # winslow.runs sink. A short message goes to the app log,
                # because the winslow logger does not reach that sink.
                workflow_logger.error(
                    f"Failed to initialize workflow '{workflow_name}': {e}",
                    exc_info=True,
                )
                self.logger.error(
                    f"Failed to initialize workflow '{workflow_name}' - see session log"
                )
                session = self.session_store.pop(session_id, None)
                if session is not None:
                    session.mark_error(e)
                await row.remove()
                await self.dashboard.add_failed_session(workflow_name, tb)
                self.notify(
                    f"Could not start '{workflow_name}': {e}",
                    title="Workflow init failed",
                    severity="error",
                )
                return

        row.complete(session)

        self.install_screen(
            screens.WorkflowScreen(session),
            name=session.screen_name,
        )

        # The bus close at session end disconnects both adapters, so no
        # explicit unsubscribe is necessary (see Workflow.archive_state).
        bus = workflow.bus
        store_adapter = TuiStoreAdapter(self, session.screen_name)
        bus.subscribe(TaskStatusEvent, store_adapter.on_task_status)
        bus.subscribe(ExecutionStatusEvent, store_adapter.on_execution_status)
        bus.subscribe(BatchCreatedEvent, store_adapter.on_batch_created)
        bus.subscribe(BatchCompletedEvent, store_adapter.on_batch_completed)
        bus.subscribe(LogLineEvent, store_adapter.on_log_line)
        lifecycle_adapter = SessionLifecycleAdapter(self, session)
        bus.subscribe(BatchCompletedEvent, lifecycle_adapter.on_batch_completed)

        cache_adapter = TuiCacheAdapter(self, session.screen_name)
        workflow.workflow_cache.add_listener(cache_adapter)
        workflow.global_cache.add_listener(cache_adapter)
        self._cache_adapters[session.session_id] = cache_adapter

    @on(SessionLifecycleEvent)
    def handle_session_lifecycle_event(self, event):
        event.apply()

    async def on_workflow_confirmation_submitted(self, message):
        await self._create_workflow(
            workflow_kls=message.workflow_kls, form_values=message.form_values
        )

    async def view_session(self, session_id):
        session = self.session_store[session_id]
        self.push_screen(session.screen_name)

    async def end_session(self, session_id):
        session = self.session_store[session_id]

        self.logger.debug(f"Ending session: {session_id} ({session.workflow})")

        # Mark the session as ended, which freezes the elapsed timer, but keep
        # the screen installed and the session in the store. The View button of
        # the History tab can thus open the workflow screen, which is now
        # read-only.
        session.actions.submit(EndSession())

        # Detach the cache adapter: the global container outlives the session
        # and would otherwise pin the dead adapter (see TuiCacheAdapter).
        if adapter := self._cache_adapters.pop(session_id, None):
            session.workflow.workflow_cache.remove_listener(adapter)
            session.workflow.global_cache.remove_listener(adapter)

    @on(Button.Pressed, ".view-dashboard")
    async def view_dashboard(self):
        await self.pop_screen()
        await self.switch_mode("dashboard")
