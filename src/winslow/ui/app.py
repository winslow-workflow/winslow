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
from winslow.session import Session
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
        workflow_name = workflow_kls.get_name()
        self.logger.debug(
            f"Workflow start message: {workflow_name} - params: {form_values}"
        )

        # Generate the session id first, so the same id names the workflow
        # logger, identifies the Session and stamps each log line. session_id
        # needs only the workflow name, which is available before the workflow
        # exists.
        session_id = generate_id(workflow_name)

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
                    orchestrator_overrides=form_values.orchestrator,
                    workflow_values=form_values.workflow,
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

        workflow.store.add_listener(TuiStoreAdapter(self, session.screen_name))
        workflow.store.add_listener(SessionLifecycleAdapter(self, session))

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
        session.end()

        # Detach the cache adapter: the global container outlives the session
        # and would otherwise pin the dead adapter (see TuiCacheAdapter).
        if adapter := self._cache_adapters.pop(session_id, None):
            session.workflow.workflow_cache.remove_listener(adapter)
            session.workflow.global_cache.remove_listener(adapter)

    @on(Button.Pressed, ".view-dashboard")
    async def view_dashboard(self):
        await self.pop_screen()
        await self.switch_mode("dashboard")
