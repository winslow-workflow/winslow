import asyncio

from textual import on
from textual.css.query import NoMatches
from textual.widgets import Button, OptionList

import winslow.ui.builtin_plugins.dashboard as dashboard_plugins

from winslow.ui.plugin import DashboardRenderContext, Slots
from winslow.ui.screens.base import SlottedScreen
from winslow.ui.builtin_plugins.dashboard.session import RestorableRow, SessionRow
from winslow.ui.builtin_plugins.dashboard.sessions import RestorableWidget
from winslow.ui.modals import WorkflowConfirmation, ErrorDetail, ForceEndModal
from winslow.ui.validation import WorkflowFormValidator, FormValues


class DashboardScreen(SlottedScreen):
    """The dashboard, rendered from the app scope of the port: descriptors,
    session rows and manifests come from the AppClient (see winslow.client)."""

    PLUGINS_MODULE = dashboard_plugins

    def __init__(self, *args, **kwargs):
        self._descriptors = None
        super().__init__(*args, **kwargs)

    @property
    def logger(self):
        return self.app.logger

    @property
    def client(self):
        return self.app.client

    @property
    def descriptors(self):
        if self._descriptors is None:
            self._descriptors = self.client.descriptors()
        return self._descriptors

    def _descriptor(self, workflow_name):
        return next(
            d for d in self.descriptors.workflows if d.workflow == workflow_name
        )

    async def on_mount(self):
        self.logger.info(f"{len(self.descriptors.workflows)} workflow classes loaded.")

        await self._populate_restorable()

        # Initialize each workflow that has Workflow.auto_init. The UI thus does
        # not show the selector and the form, which helps a test. There is no
        # value from a form: create_session starts from the parsed base of the
        # workflow, which holds each default.
        for descriptor in self.descriptors.workflows:
            if descriptor.auto_init:
                self.logger.info(f"auto_init: initializing {descriptor.workflow}")
                await self.app._create_workflow(
                    workflow_name=descriptor.workflow,
                    form_values=FormValues(),
                )

    @on(OptionList.OptionSelected, "#workflow-selector")
    def on_workflow_selected(self, event):
        prompt = event.option.prompt
        self.logger.debug(event)
        self.query("#workflow-form-placeholder").add_class("hidden")
        self.query(".workflow-form").add_class("hidden")

        form_id = f"#workflow-form-{prompt}"
        self.query(form_id).remove_class("hidden")

    def compose(self):
        context = DashboardRenderContext(
            client=self.client,
            descriptors=self.descriptors,
        )
        top_slots = (
            Slots.DASHBOARD_WORKFLOWS,
            Slots.DASHBOARD_WORKFLOW_FORM,
            Slots.DASHBOARD_SESSIONS,
        )
        yield from self._compose_slots("top-pane", top_slots, context)
        yield from self._compose_slots(
            "bottom-pane", (Slots.DASHBOARD_LOGS, Slots.DASHBOARD_RESOURCES), context
        )

    @on(Button.Pressed, ".workflow-start")
    async def start_session(self, event):
        workflow_name = event.button.name
        descriptor = self._descriptor(workflow_name)

        validator = WorkflowFormValidator(logger=self.logger)

        workflow_form = self.query_one(f"#workflow-form-{workflow_name}")
        form_values, errors = validator.validate(
            workflow_form, self.descriptors.overrides, descriptor.options
        )
        validator.render_errors(workflow_form, errors)
        if errors:
            return

        modal = WorkflowConfirmation(
            workflow=workflow_name,
            form_values=form_values,
            registry=self.plugin_registry,
        )
        self.app.push_screen(modal)

    def _session_status(self, session_id):
        return next(
            (
                row.status
                for row in self.client.sessions()
                if row.session_id == session_id
            ),
            None,
        )

    @on(Button.Pressed, ".workflow-end")
    async def end_session(self, event):
        row = next(a for a in event.button.ancestors if isinstance(a, SessionRow))
        session_id = row.session_id
        if session_id is None:
            return
        if self._session_status(session_id) == "ENDING":
            self.app.push_screen(ForceEndModal(self.client.session(session_id)))
            return
        await self.app.end_session(session_id)
        # A session with active batches drains first; the session_ended event
        # moves the row to the history when the drain completes.
        if self._session_status(session_id) == "ENDING":
            row.begin_ending()

    async def move_session_to_history(self, session_id):
        """The session_ended reaction: replace the live row with a history
        row. The app calls this from its port subscription (see
        Winslow._on_session_ended)."""
        row = next(
            (
                r
                for r in self.query(SessionRow).results()
                if r.session_id == session_id
            ),
            None,
        )
        # The end paths can race; the second call finds no row.
        if row is None or not row.is_mounted:
            return
        final = row._fetch_row()
        await row.remove()
        await self.add_history_session(final)

    @on(Button.Pressed, ".workflow-view")
    async def view_session(self, event):
        row = next(a for a in event.button.ancestors if isinstance(a, SessionRow))
        await self.app.view_session(row.session_id)

    @on(Button.Pressed, ".session-error")
    async def show_session_error(self, event):
        row = next(a for a in event.button.ancestors if isinstance(a, SessionRow))
        self.app.push_screen(ErrorDetail(row.error))

    async def _populate_restorable(self):
        """Fill the Restore pane with the open manifests of the state store.
        With nothing to restore the pane stays hidden."""
        try:
            widget = self.query_one(RestorableWidget)
        except NoMatches:
            return
        manifests = await asyncio.to_thread(self.client.manifests)
        if not manifests:
            widget.display = False
            return
        self.query("#restorable-list-placeholder").add_class("hidden")
        restore_list = self.query_one("#restorable-list")
        restore_list.remove_class("hidden")
        if len(manifests) > 1:
            await restore_list.mount(
                Button("Restore all", id="restore-all", classes="compact small")
            )
        for manifest in manifests:
            await restore_list.mount(RestorableRow(manifest))

    @on(Button.Pressed, ".session-restore")
    async def restore_session(self, event):
        row = next(a for a in event.button.ancestors if isinstance(a, RestorableRow))
        await self._restore_row(row)

    @on(Button.Pressed, "#restore-all")
    async def restore_all_sessions(self, event):
        await event.button.remove()
        for row in list(self.query(RestorableRow).results()):
            await self._restore_row(row)

    async def _restore_row(self, row):
        manifest = row.manifest
        await row.remove()
        if not self.query(RestorableRow):
            await self.query("#restore-all").remove()
            self.query_one(RestorableWidget).display = False
        await self.app._restore_session(manifest)

    async def add_pending_session(self, workflow_name) -> SessionRow:
        self.query("#session-list-placeholder").add_class("hidden")
        session_list = self.query_one("#session-list")
        session_list.remove_class("hidden")
        row = SessionRow(workflow_name)
        await session_list.mount(row)
        return row

    async def add_history_session(self, session_row) -> SessionRow:
        self.query("#history-list-placeholder").add_class("hidden")
        history_list = self.query_one("#history-list")
        history_list.remove_class("hidden")
        row = SessionRow(session_row.instance_name, row=session_row)
        await history_list.mount(row)
        return row

    async def add_failed_session(self, workflow_name, error) -> SessionRow:
        self.query("#history-list-placeholder").add_class("hidden")
        history_list = self.query_one("#history-list")
        history_list.remove_class("hidden")
        row = SessionRow(workflow_name, error=error)
        await history_list.mount(row)
        return row
