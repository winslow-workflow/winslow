import asyncio

from textual import on
from textual.widgets import Button, Label
from textual.containers import Horizontal, VerticalScroll

from winslow.actions import EndSession
from winslow.ui.actions import ACTIVE_BATCH_STATUSES
from winslow.ui.reads import port_read

from .common import BaseModal


def _active_batches(snapshot):
    return [
        batch for batch in snapshot.batches if batch.status in ACTIVE_BATCH_STATUSES
    ]


class ForceEndModal(BaseModal):
    """The confirmation before a force end: the active batches and their
    rosters. The caller passes the snapshot, roster and history DTOs, so
    the wire round-trips run off the UI thread (see DashboardScreen)."""

    def __init__(self, client, snapshot, roster, history, *args, **kwargs):
        self.client = client
        self._snapshot = snapshot
        self._roster = roster
        self._history = history
        super().__init__(*args, **kwargs)

    @property
    def modal_title(self):
        n = len(_active_batches(self._snapshot))
        return f"Force end - {n} active batch{'es' if n != 1 else ''} still running"

    def on_mount(self):
        # The session can drain by itself while this modal is open. Close the
        # modal, so the user cannot click an old Force end button for batches
        # that already completed.
        self.set_interval(1, self._dismiss_if_drained)

    async def _dismiss_if_drained(self):
        snapshot = await asyncio.to_thread(
            port_read, self, self.client.snapshot, quiet=True
        )
        if snapshot is None:
            return
        if snapshot.status == "ENDED" or not _active_batches(snapshot):
            self.dismiss()

    def compose_content(self):
        # The roster labels come from the roster read; the per-batch keys
        # from the history rows. An outage renders the batches without them.
        labels = {info.key: str(info) for info in self._roster or ()}
        rosters = {row.uuid: tuple(row.tasks) for row in self._history or ()}
        with VerticalScroll():
            for batch in _active_batches(self._snapshot):
                yield Label(
                    f"{batch.action} · {batch.task_count} task(s)",
                    classes="force-batch",
                )
                for key in rosters.get(batch.uuid, ()):
                    yield Label(f"  {labels.get(key, key)}", classes="force-task")
        with Horizontal(classes="actions"):
            yield Button("Cancel", id="force-cancel")
            yield Button.error("Force end", id="force-confirm")

    @on(Button.Pressed, "#force-confirm")
    def _confirm(self):
        self.client.submit(EndSession(force=True))
        self.dismiss()

    @on(Button.Pressed, "#force-cancel")
    def _cancel(self):
        self.dismiss()
