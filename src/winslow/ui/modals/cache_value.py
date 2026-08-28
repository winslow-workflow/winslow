import asyncio
import json
import traceback

from enum import StrEnum
from functools import partial

from rich.pretty import Pretty

from textual import on
from textual.containers import VerticalScroll
from textual.widgets import Static, Tree

from winslow.logger import LOGGER
from winslow.model import EntryState, SnapshotEncoding
from winslow.ui.modals.common import BaseModal
from winslow.ui.widgets.common.logs import HIGHLIGHTER
from winslow.util import safe_repr


# One level renders per expand, at most this many children per node, so a
# large container stays browsable.
NODE_CHILD_CAP = 200

# Bounds of a RAW rendering, so a large value cannot stall the modal.
RAW_MAX_ITEMS = 100
RAW_MAX_STRING = 500


class _View(StrEnum):
    """What the produce step returns for the modal to mount."""

    TREE = "tree"
    PRETTY = "pretty"
    TEXT = "text"
    NOTE = "note"
    # A traceback renders through the log highlighter (see HIGHLIGHTER).
    TRACEBACK = "traceback"


def _children(value):
    """The (label, child value) pairs of one container level."""
    if isinstance(value, dict):
        return [(safe_repr(key), child) for key, child in value.items()]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [(str(index), child) for index, child in enumerate(value)]
    return []


def _node_label(label, value):
    if _children(value):
        return f"{label}: {type(value).__name__}, len {len(value)}"
    return f"{label}: {safe_repr(value)}"


class CacheValue(BaseModal):
    """The value of one cache entry, rendered per its display_style. A failing
    produce step renders its traceback and logs it, so the view never hides
    the cause. Open with for_entry (live) or for_snapshot (history)."""

    def __init__(self, title, produce, logger=None, *args, **kwargs):
        self._title = title
        self._produce = produce
        self._logger = logger or LOGGER
        super().__init__(*args, **kwargs)

    @classmethod
    def for_entry(cls, client, cache_name, entry_name, logger=None):
        """The live view: read the CacheValueView through the port. The value
        arrives rendered, so the modal shows the same text a wire client
        receives. An ERRORED entry shows its context (see CacheEntryError)."""
        return cls(
            f"{cache_name}.{entry_name}",
            partial(cls._produce_entry, client, cache_name, entry_name),
            logger,
        )

    @classmethod
    def _produce_entry(cls, client, cache_name, entry_name):
        view = client.cache_value(cache_name, entry_name)
        notes = []
        if view.error is not None:
            tier = f" on {view.error.tier}" if view.error.tier else ""
            notes.append(
                f"ERRORED - the {view.error.origin} failed{tier}: "
                f"{view.error.message}"
            )
        if view.summary is not None:
            notes.append(f"bounded rendering - {view.summary}")
        note = "\n".join(notes) or None
        if view.rendered is None:
            # No value to show: the stored traceback is the whole view.
            if view.error is not None and view.error.traceback:
                return _View.TRACEBACK, view.error.traceback, note
            if view.state == EntryState.COMPUTING:
                return _View.NOTE, "<computing - the loader runs right now>", None
            return _View.NOTE, note or "<cold - no value>", None
        if view.encoding == SnapshotEncoding.JSON:
            return _View.TREE, (entry_name, json.loads(view.rendered)), note
        return _View.TEXT, view.rendered, note

    @classmethod
    def for_snapshot(cls, snapshot, logger=None):
        """The history view: a JSON snapshot deserializes back into a value
        for the tree (see SnapshotEncoding)."""
        title = f"{snapshot.cache_name}.{snapshot.entry_name} (snapshot)"
        return cls(title, partial(cls._produce_snapshot, snapshot), logger)

    @classmethod
    def _produce_snapshot(cls, snapshot):
        value = json.loads(snapshot.rendered)
        return _View.TREE, (snapshot.entry_name, value), None

    @property
    def modal_title(self):
        return self._title

    def compose_content(self):
        yield VerticalScroll(Static("loading..."), classes="value-view")

    async def on_mount(self):
        try:
            view, payload, note = await asyncio.to_thread(self._produce)
        except Exception:
            self._logger.error(
                f"The value view of '{self._title}' failed.", exc_info=True
            )
            await self._mount_content(Static(HIGHLIGHTER(traceback.format_exc())))
            return
        match view:
            case _View.TREE:
                label, value = payload
                tree = Tree("")
                self._seed(tree.root, label, value)
                tree.root.expand()
                await self._mount_content(tree, note=note)
            case _View.PRETTY:
                pretty = Pretty(
                    payload, max_length=RAW_MAX_ITEMS, max_string=RAW_MAX_STRING
                )
                await self._mount_content(Static(pretty), note=note)
            case _View.TRACEBACK:
                await self._mount_content(Static(HIGHLIGHTER(payload)), note=note)
            case _View.TEXT | _View.NOTE:
                await self._mount_content(Static(payload), note=note)

    async def _mount_content(self, widget, note=None):
        scroll = self.query_one(".value-view", VerticalScroll)
        await scroll.remove_children()
        if note is not None:
            await scroll.mount(Static(note, classes="value-error"))
        await scroll.mount(widget)

    @classmethod
    def _seed(cls, node, label, value):
        node.set_label(_node_label(label, value))
        node.data = value
        node.allow_expand = bool(_children(value))

    @on(Tree.NodeExpanded)
    def render_level(self, event):
        node = event.node
        if node.children or not node.allow_expand:
            return
        children = _children(node.data)
        for label, value in children[:NODE_CHILD_CAP]:
            child = node.add("")
            self._seed(child, label, value)
        if len(children) > NODE_CHILD_CAP:
            rest = node.add(f"... {len(children) - NODE_CHILD_CAP} more")
            rest.allow_expand = False
