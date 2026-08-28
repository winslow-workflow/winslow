import asyncio
import json
import traceback

from enum import StrEnum

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


class _View(StrEnum):
    """What _produce returns for the modal to mount."""

    TREE = "tree"
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
    """The shell of a cache value view. A subclass owns its inputs, its
    title and its _produce; the shell runs _produce off the UI thread and
    mounts the (view, payload, note) it answers. A failing _produce renders
    its traceback and logs it, so the view never hides the cause."""

    def __init__(self, logger=None, *args, **kwargs):
        self._logger = logger or LOGGER
        super().__init__(*args, **kwargs)

    def _produce(self):
        raise NotImplementedError

    def compose_content(self):
        yield VerticalScroll(Static("loading..."), classes="value-view")

    async def on_mount(self):
        try:
            view, payload, note = await asyncio.to_thread(self._produce)
        except Exception:
            self._logger.error(
                f"The value view of '{self.modal_title}' failed.", exc_info=True
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


class CacheEntryValue(CacheValue):
    """The live view: read the CacheValueView through the port. The value
    arrives rendered, so the modal shows the same text a wire client
    receives. An ERRORED entry shows its context (see CacheEntryError)."""

    def __init__(self, client, cache_name, entry_name, *args, **kwargs):
        self._client = client
        self._cache_name = cache_name
        self._entry_name = entry_name
        super().__init__(*args, **kwargs)

    @property
    def modal_title(self):
        return f"{self._cache_name}.{self._entry_name}"

    def _produce(self):
        view = self._client.cache_value(self._cache_name, self._entry_name)
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
            return _View.TREE, (self._entry_name, json.loads(view.rendered)), note
        return _View.TEXT, view.rendered, note


class CacheSnapshotValue(CacheValue):
    """The history view: a stored JSON snapshot deserializes back into a
    value for the tree (see SnapshotEncoding)."""

    def __init__(self, snapshot, *args, **kwargs):
        self._snapshot = snapshot
        super().__init__(*args, **kwargs)

    @property
    def modal_title(self):
        snapshot = self._snapshot
        return f"{snapshot.cache_name}.{snapshot.entry_name} (snapshot)"

    def _produce(self):
        value = json.loads(self._snapshot.rendered)
        return _View.TREE, (self._snapshot.entry_name, value), None
