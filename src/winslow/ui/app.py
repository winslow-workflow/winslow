import asyncio
import inspect
import traceback
from functools import partial

from textual import on
from textual.app import App, ScreenStackError
from textual.widgets import Footer, Header, Button

from winslow.ui import screens
from winslow.ui.store_adapter import SessionLifecycleEvent
from winslow.actions import EndSession
from winslow.client import LocalAppClient
from winslow.events import SessionEndedEvent
from winslow.session import SessionRegistry
from winslow.state import create_state_store


def session_screen_name(session_id):
    return f"session-{session_id}"


class Winslow(App):
    """The TUI app: the composition root. Every screen consumes the port
    surface alone (see winslow.client). Locally the app owns the session
    registry and the state store and builds the LocalAppClient over them;
    `winslow connect` passes the wire client instead, and the app touches
    no local session state."""

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

    def __init__(self, orchestrator, orchestrator_config, client=None):
        # A private name, to prevent a clash if Textual adds a config object.
        self._winslow_config = orchestrator_config
        self.orchestrator = orchestrator

        if client is None:
            self.sessions = SessionRegistry()
            # One durable store for every session of the app: manifests, task
            # snapshots and batch records live here (see winslow.state).
            self.state_store = create_state_store(orchestrator_config)
            # The session port of this process. Every screen reads, subscribes
            # and acts through it (see winslow.client).
            client = LocalAppClient(
                self.sessions, orchestrator=orchestrator, state_store=self.state_store
            )
        else:
            # A wire client: the serve process owns the registry and the store.
            self.sessions = None
            self.state_store = None
        self.client = client

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

    def on_mount(self):
        # The wire emits on its receiver thread; the message hops to the UI
        # thread. The local transport emits nothing (see subscribe_connection).
        self.client.subscribe_connection(self._relay_connection)

    def _relay_connection(self, event):
        self.post_message(
            SessionLifecycleEvent(partial(self._notify_connection, event))
        )

    def _notify_connection(self, event):
        if event.connected:
            self.notify("Reconnected to the serve process.")
        else:
            self.notify(
                "Connection to the serve process lost - reconnecting.",
                severity="warning",
            )

    @property
    def dashboard(self):
        # The dashboard is a MODE. Its base screen is not in the registry of the
        # named screens that get_screen reads, so this property reads the stack
        # of the mode directly.
        return self._screen_stacks["dashboard"][0]

    async def _create_workflow(self, workflow_name, form_values):
        await self._start_session(
            workflow_name,
            partial(
                self.client.create_session,
                workflow_name,
                form_values.orchestrator,
                form_values.workflow,
            ),
        )

    async def _restore_session(self, manifest):
        """Rebuild one session from its manifest and seed it from its
        snapshots. The dashboard offers this for every open manifest."""
        await self._start_session(
            manifest.workflow_class,
            partial(self.client.restore_session, manifest.session_id),
        )

    async def _start_session(self, workflow_name, create):
        """One session through the port: `create` is the AppClient call that
        answers a SessionRow. A failure lands in the history as a failed row
        with the traceback."""
        row_widget = await self.dashboard.add_pending_session(workflow_name)
        self.logger.info(f"Initializing workflow: {workflow_name}")
        try:
            session_row = await asyncio.to_thread(create)
        except Exception as e:
            # The session log carries the traceback through the create flow;
            # the failed row shows it for the ErrorDetail modal. A wire
            # refusal carries the server traceback (see RequestError.detail).
            tb = getattr(e, "detail", None) or traceback.format_exc()
            self.logger.error(
                f"Failed to initialize workflow '{workflow_name}': {e}"
            )
            await row_widget.remove()
            await self.dashboard.add_failed_session(workflow_name, tb)
            self.notify(
                f"Could not start '{workflow_name}': {e}",
                title="Workflow init failed",
                severity="error",
            )
            return

        self.logger.info(f"Workflow '{workflow_name}' ready.")
        row_widget.complete(session_row)
        self._connect_session(session_row)

    def _connect_session(self, session_row):
        """Install the workflow screen over one SessionClient and wire the
        session-ended lane that moves the dashboard row to the history."""
        session_id = session_row.session_id
        client = self.client.session(session_id)
        screen = screens.WorkflowScreen(client, session_row)
        self.install_screen(screen, name=session_screen_name(session_id))
        screen.connect()

        # The end event moves the dashboard row to the history. The bus
        # close at session end disconnects the lane.
        client.subscribe(
            SessionEndedEvent, partial(self._relay_session_ended, session_id)
        )

    def _relay_session_ended(self, session_id, event):
        self.post_message(
            SessionLifecycleEvent(
                partial(self.dashboard.move_session_to_history, session_id)
            )
        )

    @on(SessionLifecycleEvent)
    async def handle_session_lifecycle_event(self, event):
        result = event.apply()
        if inspect.isawaitable(result):
            await result

    async def on_workflow_confirmation_submitted(self, message):
        await self._create_workflow(
            workflow_name=message.workflow, form_values=message.form_values
        )

    async def view_session(self, session_id):
        self.push_screen(session_screen_name(session_id))

    async def end_session(self, session_id):
        self.logger.debug(f"Ending session: {session_id}")

        # The screen and the session stay installed for the History View
        # button. The screen detaches its cache lane first, so a quiet end
        # releases the workflow cache at once.
        screen = self.get_screen(session_screen_name(session_id))
        screen.prepare_session_end()
        ack = screen.client.submit(EndSession())
        if not ack.accepted:
            self.notify(ack.reason, severity="warning")

    @on(Button.Pressed, ".view-dashboard")
    async def view_dashboard(self):
        await self.pop_screen()
        await self.switch_mode("dashboard")
