from rich.syntax import Syntax
from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import (
    DataTable,
    Label,
    MarkdownViewer,
    RichLog,
    TabbedContent,
    TabPane,
    Tree,
)

from winslow.cache import SnapshotEncoding
from winslow.decorators import NOT_MATERIALIZED
from winslow.runner.execution import ExecutionPhase
from winslow.ui.modals.cache_value import CacheValue
from winslow.logger import (
    InteractiveLogHandler,
    INTERACTIVE_FORMATTER,
    get_task_dispatcher,
)
from winslow.task.info import _ambiguous_names, _location, _origin_label
from winslow.util import safe_repr
from winslow.ui.css import package_css
from winslow.ui.plugin import UIPlugin, TaskDetailRenderContext, Slots
from winslow.ui.widgets.common.logs import LogView

_CSS = package_css(__package__, "task_detail.tcss")


class TaskLogView(LogView):
    """The Logs tab. History passes the stored lines; without them the view is
    live and reads the backlog and the stream by the task uuid alone."""

    def __init__(self, task_uuid, logs=None, *args, **kwargs):
        self._task_uuid = task_uuid
        self._logs = logs
        super().__init__(*args, **kwargs)
        self._log_handler = InteractiveLogHandler(self.write)

    def on_mount(self):
        if self._logs is not None:
            for line in self._logs:
                self.write(line)
        else:
            # A record between the backlog read and the subscribe is lost. The
            # gap costs one display line at most, so the view accepts it.
            dispatcher = get_task_dispatcher()
            for record in dispatcher.buffered(self._task_uuid):
                self.write(INTERACTIVE_FORMATTER.format(record))
            dispatcher.add_listener(self._task_uuid, self._log_handler)

    def on_unmount(self):
        if self._logs is None:
            get_task_dispatcher().remove_listener(self._task_uuid, self._log_handler)


class AttributeTable(DataTable):
    """A Name/Value table that the user cannot edit. It is filled at mount,
    because a DataTable accepts no column and no row during compose."""

    def __init__(self, columns, rows, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._attr_columns = columns
        self._attr_rows = rows
        self.show_cursor = False
        self.zebra_stripes = True

    def on_mount(self):
        self.add_columns(*self._attr_columns)
        for row in self._attr_rows:
            self.add_row(*row)


class CacheReadsTable(AttributeTable):
    """The cache reads of a record. The selection of a JSON snapshot row opens
    its stored value as a tree (see CacheValue.for_snapshot)."""

    def __init__(self, columns, reads, *args, **kwargs):
        # reads: (snapshot, row) pairs, in display order. The snapshots key
        # off the row keys, so a future sort cannot open the wrong one.
        self._reads = list(reads)
        self._snapshots_by_key = {}
        super().__init__(columns, [row for _, row in self._reads], *args, **kwargs)
        self.show_cursor = True
        self.cursor_type = "row"

    def on_mount(self):
        self.add_columns(*self._attr_columns)
        for snapshot, row in self._reads:
            self._snapshots_by_key[self.add_row(*row)] = snapshot

    @on(DataTable.RowSelected)
    def open_snapshot(self, event):
        snapshot = self._snapshots_by_key[event.row_key]
        if snapshot.encoding is SnapshotEncoding.JSON:
            self.app.push_screen(CacheValue.for_snapshot(snapshot))


def _walk_nodes(node):
    yield node
    for child in node.children:
        yield from _walk_nodes(child)


class SourceTree(Tree):
    """The reverse inheritance tree for the Code tab, with the concrete task at
    the root. It is built from TaskInfo.source. Each node holds its SourceNode
    as data: the syntax pane reads .source, and the header reads .module and
    .name.

    auto_expand is off, so a click on the label of a node only selects the node.
    The arrow does the expand and the collapse. A label shows its class path in
    parentheses only if the same class name comes from more than one module, which
    means that the classes are different. The user can thus separate them."""

    def __init__(self, root_node, root_dir):
        self._root_node = root_node
        self._root_dir = root_dir
        self._ambiguous = _ambiguous_names(_walk_nodes(root_node))
        super().__init__(self._label(root_node), root_node)
        self.auto_expand = False

    def _label(self, node):
        return _origin_label(node, self._ambiguous, self._root_dir)

    def on_mount(self):
        def add(widget_node, src_node):
            for child in src_node.children:
                add(widget_node.add(self._label(child), data=child), child)

        add(self.root, self._root_node)
        self.root.expand_all()


class SourceView(Horizontal):
    """The Code tab. The inheritance tree is on the left, and the source of the
    selected class is on the right. The right side shows the root class by
    default, and it refreshes when the user selects a node of the tree.

    This is a Horizontal container with the tree and the right pane. The right
    pane is a Vertical with a path header above the syntax body, which
    scrolls."""

    def __init__(self, root_node, root_dir):
        self._root_node = root_node
        self._root_dir = root_dir
        super().__init__()

    def compose(self):
        yield SourceTree(self._root_node, self._root_dir)
        with Vertical(id="source-pane"):
            with Horizontal(id="source-header"):
                yield Label(id="source-path")
            yield RichLog(id="source-code", auto_scroll=False)

    def on_mount(self):
        self._show(self._root_node)

    def on_tree_node_selected(self, event):
        event.stop()
        self._show(event.node.data)

    def _show(self, node):
        self.query_one("#source-path", Label).update(
            f"{node.name}  ·  {_location(node.module, node.path, self._root_dir)}"
        )
        log = self.query_one("#source-code", RichLog)
        log.clear()
        log.write(
            Syntax(node.source, "python", line_numbers=True, word_wrap=False),
            expand=True,
        )


class TaskDetailWidget(Widget):
    """Every tab renders from the TaskInfo value and the record payload. A
    stub info, from a batch that still runs, has no attributes, docs or
    source; those tabs stay empty until the completion sweep replaces it."""

    DEFAULT_CSS = _CSS

    def __init__(
        self,
        info,
        logs=None,
        transient_snapshots=None,
        cache_snapshots=None,
        *args,
        **kwargs,
    ):
        self.w_info = info
        self._logs = logs
        self._transient_snapshots = transient_snapshots
        self._cache_snapshots = cache_snapshots
        super().__init__(*args, **kwargs)

    def compose(self):
        root_dir = self.app.orchestrator.directory
        info = self.w_info
        with TabbedContent(classes="main"):
            with TabPane("Logs"):
                yield TaskLogView(info.uuid, logs=self._logs)
            with TabPane("Attributes"):
                with VerticalScroll(classes="attributes"):
                    for title, columns, rows in info.attributes or ():
                        if not rows:
                            continue
                        yield Label(title, classes="attr-section")
                        yield AttributeTable(columns, rows)
                    # A transient property exists only during a batch execution.
                    # A modal at row level, from the execution history, carries
                    # the snapshots of this task for each phase, which the UI
                    # shows as a matrix of property by phase. The plain task-list
                    # view has no snapshot and thus no table. It tells the user
                    # where the snapshots are.
                    if transients := info.transients:
                        yield Label("Transient Properties", classes="attr-section")
                        snapshots = self._transient_snapshots or {}
                        # A snapshot holds each declared transient property for
                        # each phase. An entry that is not materialized would
                        # hide the real values, so only a pair with a value gets
                        # a row.
                        rows = [
                            (name, str(phase).replace("_", " "), snapshots[phase][name])
                            for name in transients
                            for phase in ExecutionPhase
                            if phase in snapshots
                            and snapshots[phase].get(name, NOT_MATERIALIZED)
                            != NOT_MATERIALIZED
                        ]
                        if rows:
                            yield AttributeTable(("Attribute", "Phase", "Value"), rows)
                        else:
                            yield Label(
                                f"{', '.join(transients)}\n\n"
                                "These are scoped to a batch execution. Check the "
                                "execution history to see their snapshots.",
                                classes="attr-note",
                            )
                    # The cache reads of this task, per phase, recorded by the
                    # runner. Only a history modal carries them (see
                    # ExecutionRecord.cache_snapshots).
                    if cache_reads := self._cache_reads():
                        yield Label("Cache Reads", classes="attr-section")
                        yield CacheReadsTable(
                            ("Entry", "Scope", "Phase", "Value"), cache_reads
                        )
            # The Documentation tab is omitted if the capture holds no markdown
            # file. An empty tab is not shown.
            if info.docs:
                with TabPane("Documentation"):
                    with TabbedContent(classes="docs"):
                        for title, text in info.docs:
                            with TabPane(title):
                                yield MarkdownViewer(text, show_table_of_contents=False)
            if info.source:
                with TabPane("Code"):
                    yield SourceView(info.source, root_dir)

    def _cache_reads(self):
        snapshots = self._cache_snapshots or {}
        return [
            (
                snap,
                (
                    f"{snap.cache_name}.{snap.entry_name}",
                    snap.scope,
                    str(phase).replace("_", " "),
                    self._cache_read_value(snap),
                ),
            )
            for phase in ExecutionPhase
            if phase in snapshots
            for snap in snapshots[phase]
        ]

    @classmethod
    def _cache_read_value(cls, snapshot):
        # One line per cell (see safe_repr). The stored rendering stays full,
        # for a later expanded view.
        match (snapshot.summary, snapshot.rendered):
            case (None, rendered):
                return safe_repr(rendered)
            case (summary, ""):
                return summary
            case (summary, rendered):
                return f"{safe_repr(rendered)} ({summary})"


class TaskDetailPlugin(UIPlugin):
    slot = Slots.TASK_DETAIL
    label = "Task Detail"

    def create_widget(self, context: TaskDetailRenderContext):
        return TaskDetailWidget(
            context.info,
            logs=context.logs,
            transient_snapshots=context.transient_snapshots,
            cache_snapshots=context.cache_snapshots,
        )
