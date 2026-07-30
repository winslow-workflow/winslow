from textual.containers import Horizontal
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Label, Button, LoadingIndicator

from winslow.session import SessionStatus
from winslow.ui.formatting import format_status_summary, format_elapsed

SUMMARY_REFRESH_INTERVAL = 2


class SessionEnded(Message):
    def __init__(self, row, session):
        super().__init__()
        self.row = row
        self.session = session


class SessionRow(Widget):
    MAX_TITLE_LENGTH = 20

    is_loading = reactive(True)

    def __init__(self, workflow_name, session=None, error=None, *args, **kwargs):
        self._workflow_name = workflow_name
        self.session = session
        self.error = error
        self._ended_posted = False
        super().__init__(*args, **kwargs)

    def _summary_label(self):
        return format_status_summary(*self.session.task_status_summary)

    def _refresh_summary(self):
        if self.session is None:
            return
        self.query_one(".summary", Label).update(self._summary_label())
        self.query_one(".elapsed", Label).update(format_elapsed(self.session.elapsed))

    def _truncate(self, title):
        if len(title) < self.MAX_TITLE_LENGTH:
            return title
        return title[: self.MAX_TITLE_LENGTH] + "..."

    def on_mount(self):
        self.border_title = self._truncate(self._workflow_name)
        # A history row has a session. A pending row gets its session at
        # complete().
        if self.session is not None:
            self.border_subtitle = self.session.workflow.identifier_suffix
        self.query_one(".waiting", Label).display = False

        if self.error:
            self.add_class("failed")
            self.query_one(".loading-state").display = False
            self.query_one(".ready-state").display = False
            self.query_one(".failed-state").display = True
            return

        # A history row is complete and its session has ended. Show the final
        # state immediately, with no timer for a live refresh.
        if self.session is not None and self.session.has_ended:
            self.query_one(".workflow-end").remove()
            self.is_loading = False
            self._refresh_summary()

    def watch_is_loading(self, loading):
        if self.error:
            return
        self.query_one(".loading-state").display = loading
        self.query_one(".ready-state").display = not loading

    def complete(self, session):
        self.session = session
        self.border_subtitle = session.workflow.identifier_suffix
        self.is_loading = False
        self._refresh_summary()
        self.set_interval(SUMMARY_REFRESH_INTERVAL, self._tick)

    @classmethod
    def _waiting_text(cls, batches):
        n = len(batches)
        tasks = sum(b.task_count for b in batches)
        return (
            f"waiting for {n} batch{'es' if n != 1 else ''} "
            f"({tasks} task{'s' if tasks != 1 else ''})..."
        )

    def begin_ending(self):
        label = self.query_one(".waiting", Label)
        label.display = True
        label.update(self._waiting_text(self.session.active_batches))

    def _tick(self):
        self._refresh_summary()
        if self.session is None:
            return
        # This is display only. The SessionLifecycleAdapter of the app does the
        # finalization, and this row only reads the outcome. The flag prevents a
        # second post while the removal is in progress.
        if self.session.has_ended:
            if not self._ended_posted:
                self._ended_posted = True
                self.post_message(SessionEnded(self, self.session))
        elif self.session.status is SessionStatus.ENDING:
            self.query_one(".waiting", Label).update(
                self._waiting_text(self.session.active_batches)
            )

    def compose(self):
        with Horizontal(classes="loading-state"):
            yield LoadingIndicator()
            yield Label("Initializing...", classes="init-label")
        with Horizontal(classes="ready-state"):
            with Horizontal(classes="totals"):
                yield Label("", classes="summary", markup=True)
                yield Label("", classes="elapsed")
                yield Label("", classes="waiting")
            with Horizontal(classes="actions"):
                yield Button(
                    "View", variant="primary", classes="compact small workflow-view"
                )
                yield Button.error("End", classes="compact small workflow-end")
        with Horizontal(classes="failed-state"):
            yield Label("Failed to initialize", classes="failed-label")
            with Horizontal(classes="actions"):
                yield Button.error("Error", classes="compact small session-error")
