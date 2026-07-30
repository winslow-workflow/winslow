from textual import on
from textual.widgets import Button, OptionList

import winslow.ui.builtin_plugins.dashboard as dashboard_plugins

from winslow.session import SessionStatus
from winslow.ui.plugin import DashboardRenderContext, Slots
from winslow.ui.screens.base import SlottedScreen
from winslow.ui.builtin_plugins.dashboard.session import SessionRow, SessionEnded
from winslow.ui.modals import WorkflowConfirmation, ErrorDetail, ForceEndModal
from winslow.ui.validation import WorkflowFormValidator, FormValues


class DashboardScreen(SlottedScreen):
    PLUGINS_MODULE = dashboard_plugins

    @property
    def logger(self):
        return self.app.orchestrator.logger

    @property
    def orchestrator(self):
        return self.app.orchestrator

    async def on_mount(self):
        self.logger.info(f"{len(self.app.workflow_context)} workflow classes loaded.")

        # Initialize each workflow that has Workflow.auto_init. The UI thus does
        # not show the selector and the form, which helps a test. There is no
        # value from a form: initialize_workflow starts from the parsed base of
        # the workflow, which holds each default.
        for workflow_kls in self.app.workflow_context:
            if workflow_kls.auto_init:
                self.logger.info(f"auto_init: initializing {workflow_kls.get_name()}")
                await self.app._create_workflow(
                    workflow_kls=workflow_kls,
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
            orchestrator=self.app.orchestrator,
            workflow_context=self.app.workflow_context,
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
        workflow_kls = self.app.workflow_name_map[workflow_name]

        validator = WorkflowFormValidator(logger=self.logger)

        workflow_form = self.query_one(f"#workflow-form-{workflow_name}")
        form_values, errors = validator.validate(
            workflow_form, self.orchestrator.config_meta, workflow_kls.config_meta
        )
        validator.render_errors(workflow_form, errors)
        if errors:
            return

        modal = WorkflowConfirmation(
            workflow_kls=workflow_kls,
            form_values=form_values,
            registry=self.plugin_registry,
        )
        self.app.push_screen(modal)

    @on(Button.Pressed, ".workflow-end")
    async def end_session(self, event):
        row = next(a for a in event.button.ancestors if isinstance(a, SessionRow))
        session = row.session
        if session is None:
            return
        if session.status is SessionStatus.ENDING:
            self.app.push_screen(ForceEndModal(session))
            return
        await self.app.end_session(session.session_id)
        if session.status is SessionStatus.ENDING:
            row.begin_ending()
        else:
            await self._move_to_history(row, session)

    @on(SessionEnded)
    async def _on_session_ended(self, event):
        await self._move_to_history(event.row, event.session)

    async def _move_to_history(self, row, session):
        # The silent end path and the tick of the row can both arrive here for the
        # same session. The second one finds no row.
        if not row.is_mounted:
            return
        await row.remove()
        await self.add_history_session(session)

    @on(Button.Pressed, ".workflow-view")
    async def view_session(self, event):
        row = next(a for a in event.button.ancestors if isinstance(a, SessionRow))
        await self.app.view_session(row.session.session_id)

    @on(Button.Pressed, ".session-error")
    async def show_session_error(self, event):
        row = next(a for a in event.button.ancestors if isinstance(a, SessionRow))
        self.app.push_screen(ErrorDetail(row.error))

    async def add_pending_session(self, workflow_name) -> SessionRow:
        self.query("#session-list-placeholder").add_class("hidden")
        session_list = self.query_one("#session-list")
        session_list.remove_class("hidden")
        row = SessionRow(workflow_name)
        await session_list.mount(row)
        return row

    async def add_history_session(self, session) -> SessionRow:
        self.query("#history-list-placeholder").add_class("hidden")
        history_list = self.query_one("#history-list")
        history_list.remove_class("hidden")
        row = SessionRow(session.workflow_name, session=session)
        await history_list.mount(row)
        return row

    async def add_failed_session(self, workflow_name, error) -> SessionRow:
        self.query("#history-list-placeholder").add_class("hidden")
        history_list = self.query_one("#history-list")
        history_list.remove_class("hidden")
        row = SessionRow(workflow_name, error=error)
        await history_list.mount(row)
        return row
