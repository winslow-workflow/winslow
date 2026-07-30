import inspect
import os
from functools import cached_property, lru_cache

from rich.syntax import Syntax
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

from winslow.decorators import declared_transient_properties, NOT_MATERIALIZED
from winslow.runner.execution import ExecutionPhase
from winslow.logger import (
    InteractiveLogHandler,
    INTERACTIVE_FORMATTER,
    get_task_dispatcher,
)
from winslow.util import safe_repr
from winslow.ui.css import package_css
from winslow.ui.plugin import UIPlugin, TaskDetailRenderContext, Slots
from winslow.ui.widgets.common.logs import LogView

_CSS = package_css(__package__, "task_detail.tcss")


class TaskLogView(LogView):
    def __init__(self, task, logs=None, *args, **kwargs):
        self.w_task = task
        self._logs = logs
        super().__init__(*args, **kwargs)
        self._log_handler = InteractiveLogHandler(self.write)

    def on_mount(self):
        if self._logs is not None:
            for line in self._logs:
                self.write(line)
        else:
            if buffered := self.w_task.buffered_logs:
                for record in buffered:
                    self.write(INTERACTIVE_FORMATTER.format(record))
            get_task_dispatcher().add_listener(id(self.w_task), self._log_handler)

    def on_unmount(self):
        if self._logs is None:
            get_task_dispatcher().remove_listener(id(self.w_task), self._log_handler)


_VALUE_LIMIT = 100


def _eval(task, name, limit=_VALUE_LIMIT):
    """The trimmed value of an attribute, which can be calculated. An error is
    caught: the getter of a property can raise, and that is information for the
    user."""
    try:
        return safe_repr(getattr(task, name), limit)
    except Exception as exc:
        msg = f"<error: {type(exc).__name__}: {exc}>"
        return msg if len(msg) <= limit else msg[: limit - 1] + "…"


_DOC_EXTENSIONS = (".md", ".markdown")


@lru_cache(maxsize=None)
def _docs_in(directory):
    """A (title, markdown_text) pair for each markdown file in the directory,
    ordered by file name and independent of the case. The title is the file name
    with no extension. The result is cached per directory: the docs do not change
    during a session, and many tasks share a directory, so each file is read one
    time (compare info._safe_source)."""
    try:
        names = sorted(os.listdir(directory), key=str.lower)
    except OSError:
        return ()
    docs = []
    for name in names:
        if not name.lower().endswith(_DOC_EXTENSIONS):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            text = f"*Could not read `{name}`: {exc}*"
        docs.append((os.path.splitext(name)[0], text))
    return tuple(docs)


def _task_docs(task):
    """The markdown docs in the directory of the source file of the task. The
    result is empty if the source directory does not resolve."""
    try:
        source = inspect.getsourcefile(type(task))
    except TypeError:
        source = None
    if not source:
        return ()
    return _docs_in(os.path.dirname(source))


def _origin(obj):
    """Normalize a class or a SourceNode into (name, module, source_path). These
    are the data that name a definition and find its source file. The location
    helper and the ambiguity helper thus serve the tree of the Code tab and also
    the Source column of the Attributes tab."""
    if isinstance(obj, type):
        try:
            path = inspect.getsourcefile(obj)
        except TypeError:
            path = None
        return obj.__name__, obj.__module__, path
    return obj.name, obj.module, obj.path


def _location(module, path, root_dir):
    """The location of a definition. For a definition in the project this is a
    path relative to the project root. For a framework definition or an installed
    definition this is the dotted module, because its absolute path is outside the
    root and gives the user no help."""
    if path:
        rel = os.path.relpath(path, root_dir)
        if not rel.startswith(".."):
            return rel
    return module


def _ambiguous_names(origins):
    """The names in the given classes and SourceNodes that more than one module
    declares. Only these names need a location to make them different."""
    modules = {}
    for obj in origins:
        name, module, _ = _origin(obj)
        modules.setdefault(name, set()).add(module)
    return {name for name, mods in modules.items() if len(mods) > 1}


def _origin_label(obj, ambiguous, root_dir):
    """The name of an origin. The location follows the name, in parentheses, only
    if the name is ambiguous, which means that more than one module uses it. The
    user can thus separate the two names."""
    name, module, path = _origin(obj)
    if name in ambiguous:
        return f"{name} ({_location(module, path, root_dir)})"
    return name


def _is_data_attribute(val):
    """True for a plain class-level value that the Class Attributes table shows.
    Such a value is not callable and is not a descriptor. The descriptor test
    removes a method, a classmethod, a staticmethod, a property, a
    cached_property, and the Parameter and TransientProperty descriptors. A list
    of those types is thus unnecessary."""
    if callable(val):
        return False
    return not any(
        hasattr(val, dunder) for dunder in ("__get__", "__set__", "__delete__")
    )


def _classify_members(task):
    """Walk the MRO of the task class one time and select the members that the UI
    shows. The match is positive: a @cached_property, a @property and a plain data
    attribute. Each other member, such as a method, a parameter descriptor, a
    transient descriptor or a nested class, does not match and is dropped. A name
    with an underscore prefix, which is private or internal, is also skipped. The
    first definition wins, so an override in a subclass hides its base. That class
    is the source of the member, and the function records it with the member. The
    UI can thus show where an inherited attribute comes from.

    A transient_property member is not included. Its value is scoped to one
    execution phase of a batch and is removed when the batch completes, so there
    is nothing to show later. Log such a value to debug it.

    Each group maps a name to (source_class, value). The value is the raw class
    attribute for a data attribute, and the descriptor object for a property or a
    cached property. The caller evaluates a descriptor against the instance."""
    class_attrs, properties, cached = {}, {}, {}
    seen = set()
    for klass in type(task).__mro__:
        if klass is object:
            continue
        for name, val in vars(klass).items():
            if name.startswith("_") or name in seen:
                continue
            seen.add(name)
            if isinstance(val, cached_property):
                cached[name] = (klass, val)
            elif isinstance(val, property):
                properties[name] = (klass, val)
            elif _is_data_attribute(val):
                class_attrs[name] = (klass, val)
    return class_attrs, properties, cached


def _attribute_sections(task, root_dir):
    """A (title, columns, rows) triple for each attribute category, in display
    order. A name with an underscore prefix, which is private or internal, is
    never included. A member that can be inherited, which is a class attribute, a
    property method or a cached-property method, starts with a Source column. That
    column names the class that declares the member, with its location if the name
    collides between the bases. These sections are ordered by (source, name). The
    config, the parameterization and the instance attributes are flat Name/Value
    tables, ordered by name.

    The instance attributes are read before the property sections. The evaluation
    of a @cached_property here thus does not put its new cached value into the
    table of the instance attributes."""
    class_attrs, properties, cached = _classify_members(task)
    ambiguous = _ambiguous_names(k for k in type(task).__mro__ if k is not object)
    label = lambda klass: _origin_label(klass, ambiguous, root_dir)
    by_source_name = lambda kv: (
        label(kv[1][0]),
        kv[0],
    )  # (name, (klass, val)) -> (label, name)

    yield (
        "Class Attributes",
        ("Source", "Name", "Value"),
        [
            (label(klass), n, safe_repr(v))
            for n, (klass, v) in sorted(class_attrs.items(), key=by_source_name)
        ],
    )
    if task._is_parameterized:
        yield (
            "Parameterization",
            ("Name", "Value"),
            [(n, safe_repr(v)) for n, v in sorted(task._parameters_dict.items())],
        )
    yield (
        "Instance Attributes",
        ("Name", "Value"),
        [
            (n, safe_repr(v))
            for n, v in sorted(vars(task).items())
            if not n.startswith("_") and n != "workflow_config"
        ],
    )
    yield (
        "Property Methods",
        ("Source", "Name", "Value"),
        [
            (label(klass), n, _eval(task, n))
            for n, (klass, _) in sorted(properties.items(), key=by_source_name)
        ],
    )
    yield (
        "Cached Property Methods",
        ("Source", "Name", "Value"),
        [
            (label(klass), n, _eval(task, n))
            for n, (klass, _) in sorted(cached.items(), key=by_source_name)
        ],
    )


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


def _walk_nodes(node):
    yield node
    for child in node.children:
        yield from _walk_nodes(child)


class SourceTree(Tree):
    """The reverse inheritance tree for the Code tab, with the concrete task at
    the root. It is built from TaskInfo.source_tree(). Each node holds its
    SourceNode as data: the syntax pane reads .source, and the header reads
    .module and .name.

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
    DEFAULT_CSS = _CSS

    def __init__(self, task, logs=None, transient_snapshots=None, *args, **kwargs):
        self.w_task = task
        self._logs = logs
        self._transient_snapshots = transient_snapshots
        super().__init__(*args, **kwargs)

    def compose(self):
        root_dir = self.app.orchestrator.directory
        docs = _task_docs(self.w_task)
        with TabbedContent(classes="main"):
            with TabPane("Logs"):
                yield TaskLogView(self.w_task, logs=self._logs)
            with TabPane("Attributes"):
                with VerticalScroll(classes="attributes"):
                    for title, columns, rows in _attribute_sections(
                        self.w_task, root_dir
                    ):
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
                    if transients := declared_transient_properties(type(self.w_task)):
                        yield Label("Transient Properties", classes="attr-section")
                        snapshots = self._transient_snapshots or {}
                        # A snapshot holds each declared transient property for
                        # each phase. An entry that is not materialized would
                        # hide the real values, so only a pair with a value gets
                        # a row.
                        rows = [
                            (name, str(phase).replace("_", " "), snapshots[phase][name])
                            for name in sorted(transients)
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
            # The Documentation tab is omitted if the directory of the task holds
            # no markdown file. An empty tab is not shown.
            if docs:
                with TabPane("Documentation"):
                    with TabbedContent(classes="docs"):
                        for title, text in docs:
                            with TabPane(title):
                                yield MarkdownViewer(text, show_table_of_contents=False)
            with TabPane("Code"):
                yield SourceView(self.w_task.info.source_tree(), root_dir)


class TaskDetailPlugin(UIPlugin):
    slot = Slots.TASK_DETAIL
    label = "Task Detail"

    def create_widget(self, context: TaskDetailRenderContext):
        return TaskDetailWidget(
            context.task,
            logs=context.logs,
            transient_snapshots=context.transient_snapshots,
        )
