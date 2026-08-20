import inspect
import os
from functools import cached_property, lru_cache

from dataclasses import dataclass

from winslow.util import safe_repr

# The types that Pretty renders directly. Any other value becomes a safe
# string, because the repr of an unknown object can raise or be huge.
_PLAIN_TYPES = (str, int, float, bool, type(None))

_VALUE_LIMIT = 100

# The value of a property that no automatic capture evaluated (see
# TaskInfo.from_task). Only the on-demand capture runs a getter.
NOT_EVALUATED = "<not evaluated>"


def _display_parameters(task):
    return {
        name: value if isinstance(value, _PLAIN_TYPES) else safe_repr(value)
        for name, value in task._parameters_dict.items()
    }


# The class-keyed caches are safe only because the scoped import machinery
# reuses modules, so each class is created one time. A re-import must clear them.
@lru_cache(maxsize=None)
def _safe_source(cls):
    """The source of a class, or None if it has none, such as object or a C-level
    struct. The result is cached by class, because inspect.getsource reads the
    source file."""
    try:
        return inspect.getsource(cls)
    except (OSError, TypeError):
        return None


def _safe_sourcefile(cls):
    """The absolute path of the file that declares a class, or None if there is
    none. It uses the real __file__ through inspect, so it does not use the
    synthetic scoped module name. It also works for a base class, which the
    registry does not collect."""
    try:
        path = inspect.getsourcefile(cls)
    except TypeError:
        return None
    return os.path.abspath(path) if path else None


@dataclass(frozen=True)
class SourceNode:
    """A node in the inheritance source tree of a task."""

    name: str
    module: str
    source: str
    path: str  # absolute source file, or None
    children: tuple  # tuple[SourceNode, ...]


@lru_cache(maxsize=None)
def _source_tree(cls):
    """The inheritance source tree of a class. The cache is at module level and
    the key is the class, so 200 instances of one parameterized task build it one
    time. The recursion also shares a cached subtree between task classes, for
    example the Task leaf.

    The function walks __bases__ and skips object and each C-level class, which
    has no source. It stops at the Task base. Task is a node in the tree, but its
    framework bases are not."""
    from winslow.task.task import Task

    children = ()
    if cls is not Task:  # Task is the boundary for internal classes
        children = tuple(
            _source_tree(base)
            for base in cls.__bases__
            if base is not object and _safe_source(base) is not None
        )
    return SourceNode(
        name=cls.__name__,
        module=cls.__module__,
        source=_safe_source(cls) or "",
        path=_safe_sourcefile(cls),
        children=children,
    )


_DOC_EXTENSIONS = (".md", ".markdown")


@lru_cache(maxsize=None)
def _docs_in(directory):
    """A (title, markdown_text) pair for each markdown file in the directory,
    ordered by file name and independent of the case. The title is the file name
    with no extension. The result is cached per directory: the docs do not change
    during a session, and many tasks share a directory, so each file is read one
    time (compare _safe_source). The session end clears this cache, so the
    next session reads an edited doc again (see release_session_caches)."""
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


def release_session_caches():
    """Clear the caches whose data can change between two sessions of one
    process. The class-keyed source caches stay (see _safe_source)."""
    _docs_in.cache_clear()


def _task_docs(task):
    """The markdown docs in the directory of the source file of the task. The
    result is empty if the source directory does not resolve."""
    source = _safe_sourcefile(type(task))
    if not source:
        return ()
    return _docs_in(os.path.dirname(source))


def _eval(task, name, limit=_VALUE_LIMIT):
    """The trimmed value of an attribute, which can be calculated. An error is
    caught: the getter of a property can raise, and that is information for the
    user."""
    try:
        return safe_repr(getattr(task, name), limit)
    except Exception as exc:
        msg = f"<error: {type(exc).__name__}: {exc}>"
        return msg if len(msg) <= limit else msg[: limit - 1] + "…"


def _origin(obj):
    """Normalize a class or a SourceNode into (name, module, source_path). These
    are the data that name a definition and find its source file. The location
    helper and the ambiguity helper thus serve the tree of the Code tab and also
    the Source column of the Attributes tab."""
    if isinstance(obj, type):
        return obj.__name__, obj.__module__, _safe_sourcefile(obj)
    return obj.name, obj.module, obj.path


def _location(module, path, root_dir):
    """The location of a definition. For a definition in the project this is a
    path relative to the project root. For a framework definition, an installed
    definition or an unknown root this is the dotted module."""
    if path and root_dir:
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
    removes a method, a classmethod, a property, a cached_property, and the
    Parameter and TransientProperty descriptors. A list of those types is thus
    unnecessary."""
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
    cached property."""
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


def _descriptor_value(task, name, evaluate):
    """The display value of a property or a cached property. A cold descriptor
    is evaluated only on request: an automatic capture must never run a
    getter, because a materialization outside the task flow changes behavior."""
    if name in vars(task):
        return safe_repr(vars(task)[name], _VALUE_LIMIT)
    if evaluate:
        return _eval(task, name)
    return NOT_EVALUATED


def _attribute_sections(task, root_dir=None, evaluate=False):
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

    sections = [
        (
            "Class Attributes",
            ("Source", "Name", "Value"),
            tuple(
                (label(klass), n, safe_repr(v))
                for n, (klass, v) in sorted(class_attrs.items(), key=by_source_name)
            ),
        )
    ]
    if task._is_parameterized:
        sections.append(
            (
                "Parameterization",
                ("Name", "Value"),
                tuple(
                    (n, safe_repr(v)) for n, v in sorted(task._parameters_dict.items())
                ),
            )
        )
    sections.append(
        (
            "Instance Attributes",
            ("Name", "Value"),
            tuple(
                (n, safe_repr(v))
                for n, v in sorted(vars(task).items())
                if not n.startswith("_") and n != "workflow_config"
            ),
        )
    )
    sections.append(
        (
            "Property Methods",
            ("Source", "Name", "Value"),
            tuple(
                (label(klass), n, _descriptor_value(task, n, evaluate))
                for n, (klass, _) in sorted(properties.items(), key=by_source_name)
            ),
        )
    )
    sections.append(
        (
            "Cached Property Methods",
            ("Source", "Name", "Value"),
            tuple(
                (label(klass), n, _descriptor_value(task, n, evaluate))
                for n, (klass, _) in sorted(cached.items(), key=by_source_name)
            ),
        )
    )
    return tuple(sections)


@dataclass(frozen=True)
class TaskRef:
    """A renderable pointer to another task: what a dependency row needs. No
    nested dependencies, so a TaskInfo stays bounded on a deep graph."""

    key: str
    label: str
    is_premier: bool
    is_terminal: bool
    is_noop: bool

    def __str__(self):
        return self.label

    @classmethod
    def from_task(cls, task):
        return cls(
            key=task.identity_key,
            label=str(task),
            is_premier=task.is_premier,
            is_terminal=task.is_terminal,
            is_noop=task.is_noop,
        )


@dataclass(frozen=True, eq=False)
class TaskInfo:
    """The value view model of a task: plain values only, so asdict is
    JSON-serializable and history can hold it without a retention of the task.

    from_task has two depths. The stub, the default, carries the identity and
    the dependency refs. The full capture adds attributes, docs, source and
    transients, and it evaluates a getter only with evaluate=True, which only
    the on-demand detail view passes. Equality and hash use the key."""

    key: str
    label: str
    name: str
    is_premier: bool
    is_terminal: bool
    is_noop: bool
    task_class: str
    index: int
    groups: tuple = ()
    parameters: dict = None
    dependencies: tuple = ()
    premier_dependencies: tuple = ()
    terminal_dependencies: tuple = ()
    # The trust fields of the check_ttl rule, from the session snapshots. None
    # means no verification on record, or no TTL (see Workflow.task_info).
    checked_at: float = None
    effective_ttl: float = None
    # Full-capture fields. None marks a stub, an empty tuple marks a capture
    # that found nothing.
    attributes: tuple = None
    docs: tuple = None
    source: SourceNode = None
    transients: tuple = None

    def __str__(self):
        return self.label

    def __hash__(self):
        return hash(self.key)

    def __eq__(self, other):
        if not isinstance(other, TaskInfo):
            return NotImplemented
        return self.key == other.key

    def get_name(self):
        return self.name

    def get_groups(self):
        return frozenset(self.groups)

    @property
    def groups_readable(self):
        return ", ".join(self.groups) if self.groups else None

    @classmethod
    def from_task(
        cls,
        task,
        full=False,
        evaluate=False,
        root_dir=None,
        checked_at=None,
        effective_ttl=None,
    ):
        from winslow.decorators import declared_transient_properties

        full = full or evaluate
        deps = task.dependent_tasks
        task_cls = task.__class__

        return cls(
            key=task.identity_key,
            checked_at=checked_at,
            effective_ttl=effective_ttl,
            label=str(task),
            name=task.instance_name,
            is_terminal=task.is_terminal,
            is_premier=task.is_premier,
            is_noop=task.is_noop,
            index=task._index,
            # The real source file through inspect, and not the synthetic scoped
            # __module__.
            task_class=f"{task_cls.__qualname__} ({_safe_sourcefile(task_cls) or task_cls.__module__})",
            groups=tuple(sorted(task.get_groups())),
            parameters=_display_parameters(task) or None,
            dependencies=tuple(
                TaskRef.from_task(d)
                for d in deps
                if not (d.is_premier or d.is_terminal)
            ),
            premier_dependencies=tuple(
                TaskRef.from_task(d) for d in deps if d.is_premier
            ),
            terminal_dependencies=tuple(
                TaskRef.from_task(d) for d in deps if d.is_terminal
            ),
            attributes=_attribute_sections(task, root_dir, evaluate) if full else None,
            docs=_task_docs(task) if full else None,
            source=_source_tree(task_cls) if full else None,
            transients=(
                tuple(sorted(declared_transient_properties(task_cls))) if full else None
            ),
        )
