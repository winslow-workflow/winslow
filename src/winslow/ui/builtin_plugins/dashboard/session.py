import asyncio

from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Label, Button, LoadingIndicator

from winslow.ui.actions import ACTIVE_BATCH_STATUSES
from winslow.ui.formatting import format_status_summary, format_elapsed
from winslow.ui.reads import port_read

SUMMARY_REFRESH_INTERVAL = 2


class RestorableRow(Widget):
    """One open manifest on the dashboard: the session that a dead process
    left behind, with its restore action. `manifest` is a ManifestRow value
    (see AppClient.manifests)."""

    def __init__(self, manifest, *args, **kwargs):
        self.manifest = manifest
        super().__init__(*args, **kwargs)

    def on_mount(self):
        self.border_title = self.manifest.workflow_class

    def compose(self):
        with Horizontal(classes="row-content"):
            yield Label(self.manifest.session_id, classes="session-id")
            with Horizontal(classes="actions"):
                yield Button(
                    "Restore",
                    variant="primary",
                    classes="compact small session-restore",
                )


class SessionRow(Widget):
    """One session on the dashboard, rendered from its SessionRow value. The
    tick refreshes the value through the AppClient of the app."""

    MAX_TITLE_LENGTH = 20

    is_loading = reactive(True)

    def __init__(self, workflow_name, row=None, error=None, *args, **kwargs):
        self._workflow_name = workflow_name
        self.row = row
        self.error = error
        super().__init__(*args, **kwargs)

    @property
    def session_id(self):
        return self.row.session_id if self.row is not None else None

    @property
    def has_ended(self):
        return self.row is not None and self.row.status in ("ENDED", "ERROR")

    def _refresh_summary(self):
        if self.row is None:
            return
        summary = self.row.task_status_summary
        self.query_one(".summary", Label).update(
            format_status_summary(
                summary.completed, summary.problematic, summary.total
            )
        )
        self.query_one(".elapsed", Label).update(format_elapsed(self.row.elapsed))

    def _truncate(self, title):
        if len(title) < self.MAX_TITLE_LENGTH:
            return title
        return title[: self.MAX_TITLE_LENGTH] + "..."

    def on_mount(self):
        self.border_title = self._truncate(self._workflow_name)
        # A history row has a session value. A pending row gets its value at
        # complete().
        if self.row is not None:
            self.border_subtitle = self.row.identifier_suffix
        self.query_one(".waiting", Label).display = False

        if self.error:
            self.add_class("failed")
            self.query_one(".loading-state").display = False
            self.query_one(".ready-state").display = False
            self.query_one(".failed-state").display = True
            return

        # A history row is complete and its session has ended. Show the final
        # state immediately, with no timer for a live refresh.
        if self.has_ended:
            self.query_one(".workflow-end").remove()
            self.is_loading = False
            self._refresh_summary()

    def watch_is_loading(self, loading):
        if self.error:
            return
        self.query_one(".loading-state").display = loading
        self.query_one(".ready-state").display = not loading

    def complete(self, row):
        self.row = row
        self.border_subtitle = row.identifier_suffix
        self.is_loading = False
        self._refresh_summary()
        # The tick only refreshes the display; the app moves the row to the
        # history on the session_ended event (see Winslow._connect_session).
        self.set_interval(SUMMARY_REFRESH_INTERVAL, self._tick)

    @classmethod
    def _waiting_text(cls, batches):
        n = len(batches)
        tasks = sum(b.task_count for b in batches)
        return (
            f"waiting for {n} batch{'es' if n != 1 else ''} "
            f"({tasks} task{'s' if tasks != 1 else ''})..."
        )

    def _active_batches(self):
        """The active batch rows, or None when the read fails (an outage
        skips one refresh; the tick reads again). Runs off the UI thread
        (see _tick)."""
        client = self.app.client.session(self.session_id)
        snapshot = port_read(self, client.snapshot, quiet=True)
        if snapshot is None:
            return None
        return [b for b in snapshot.batches if b.status in ACTIVE_BATCH_STATUSES]

    async def begin_ending(self):
        label = self.query_one(".waiting", Label)
        label.display = True
        batches = await asyncio.to_thread(self._active_batches)
        if batches is not None:
            label.update(self._waiting_text(batches))

    def fetch_row(self):
        """The current SessionRow value of this session, or the last known
        one when the read fails or finds no match. Runs off the UI thread
        (see _tick)."""
        rows = port_read(self, self.app.client.sessions, quiet=True)
        if rows is None:
            return self.row
        return next((r for r in rows if r.session_id == self.session_id), self.row)

    async def _tick(self):
        if self.row is None:
            return
        self.row = await asyncio.to_thread(self.fetch_row)
        self._refresh_summary()
        if self.row.status == "ENDING":
            batches = await asyncio.to_thread(self._active_batches)
            if batches is not None:
                self.query_one(".waiting", Label).update(self._waiting_text(batches))

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
