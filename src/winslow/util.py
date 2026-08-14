import os
import re
import sys
import time
import uuid
import hashlib
import builtins
import threading
import importlib
import importlib.util
import importlib.machinery
import inspect
import contextvars

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager


def generate_id(name):
    """Make a readable and sortable identifier that has no collision. It has
    three parts: a name for the user, a UTC timestamp and a random suffix. The
    timestamp sorts the ids, and also the per-session log files that use them.
    The suffix makes each id unique. For example, generate_id("alpha") returns
    "alpha-20260708T143052-a1b2c3d4". The name is used without a change, so it
    round-trips. A sink with a limited charset sanitizes the name at its own
    boundary."""
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    return f"{name}-{stamp}-{uuid.uuid4().hex[:8]}"


def get_meta(klass):
    """Return the Meta inner class that is declared on klass, or None. This does
    not walk the MRO. A subclass with no Meta of its own must not inherit
    abstract=True or another class-level flag from a parent."""
    return klass.__dict__.get("Meta")


def get_is_abstract(klass):
    meta = get_meta(klass)
    return meta is not None and getattr(meta, "abstract", False)


def safe_repr(value, limit=100):
    """One-line display form: collapsed whitespace, limited length, safe repr.
    A string renders without quotes unless it would render as nothing."""
    if isinstance(value, str):
        s = " ".join(value.split())
        if not s:
            s = repr(value)
        return s if len(s) <= limit else s[: limit - 1] + "…"
    try:
        s = repr(value)
    except Exception as exc:
        s = f"<unrepresentable: {type(exc).__name__}: {exc}>"
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def is_tuple_like(value):
    return isinstance(value, Iterable) and not isinstance(value, str)


def to_tuple(value):
    """Convert a sequence or a single value into a tuple."""

    if isinstance(value, tuple):
        return value

    elif is_tuple_like(value):
        return tuple(value)

    return (value,)


def flatten(iterable):

    def _flat(objects):
        for obj in objects:
            if is_tuple_like(obj):
                yield from _flat(obj)
            else:
                yield obj

    return tuple(_flat(iterable))


def _camel_split(s, sep):
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", rf"\g<1>{sep}\g<2>", s)
    s = re.sub(r"([a-z\d])([A-Z])", rf"\g<1>{sep}\g<2>", s)
    return s.lower()


def camel_to_kebab(s):
    return _camel_split(s, "-")


def camel_to_snake(s):
    return _camel_split(s, "_")


def slugify(s, max_length=48):
    """Reduce a display string to [a-z0-9_-], for a path or a label. Lossy on
    purpose - pair it with a digest where uniqueness matters."""
    s = re.sub(r"[^a-z0-9_-]+", "-", s.lower()).strip("-")
    return s[:max_length].rstrip("-") or "_"


def _ensure_package(name, path):
    """Register `name` as a package with `path` as its __path__. The scoped
    module hierarchy is thus a real hierarchy, and a relative import in the task
    tree of a workflow resolves, for example `from ..build.compile import X`.
    This function is idempotent."""
    if name in sys.modules:
        return
    spec = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
    spec.submodule_search_locations = [path]
    sys.modules[name] = importlib.util.module_from_spec(spec)


# This prefix marks a directory-scoped workflow package (see iter_dir_modules).
# The scoped __import__ acts only on a caller whose package is below it.
_SCOPE_NS = "_winslow_"

# This lock protects the process-global scope state: the import hook and its
# unwind chain. It is an RLock, because isolated_scopes holds it for its full
# body, and the workflow load inside that body takes it again through
# _install_scoped_import.
_scope_lock = threading.RLock()


def _has_submodule(package_name, child):
    """True if `child` is a direct submodule or subpackage of the loaded package
    `package_name`. The test reads the real __path__ on the disk."""
    package = sys.modules.get(package_name)
    if package is None:
        return False
    for path in getattr(package, "__path__", ()):
        if os.path.isdir(os.path.join(path, child)) or os.path.isfile(
            os.path.join(path, child + ".py")
        ):
            return True
    return False


def _install_scoped_import():
    """Wrap ``builtins.__import__``, so the absolute imports of a workflow module
    route into its own workflow package. All other imports stay unchanged. This
    function is idempotent.

    The problem: a workflow keeps its tasks in a directory such as ``tasks/`` and
    writes a usual absolute import, for example ``from tasks.deploy import X``.
    This is correct in one workflow. But ``tasks`` is one global key in
    ``sys.modules``. When many workflows load in one process (auto_init, the
    registry), they collide on that key. The hook rewrites such an import to its
    scoped name, so each import resolves in its own workflow and never through a
    shared global ``tasks``.

    The hook is global and does not replace the builtins of each module, because
    ``__import__`` is the only interception point that receives the globals of
    the caller. It can thus find the workflow that does the import. It also works
    for a submodule that another module imports, which never carries the builtins
    of winslow.

    The workflow root is resolved at each call. It is the nearest ancestor
    package that owns the imported head as a child. The same hook thus works for
    a workflow that is loaded directly, where the scope is the workflow
    directory, and for a workflow that is a subpackage of a walked tree. An
    example of the second case is the registry, which walks a root of many
    workflows and finds the workflow root one level below.

    A relative import (level > 0) is already scoped through ``__package__`` and
    is never rewritten. A caller outside a scope, or an import of a name that no
    scope owns, such as the stdlib or ``winslow``, goes directly to the real
    importer.

    Example: the caller is ``_winslow_ab12.beta.tasks.build``, and beta/ contains
    tasks/::

        from tasks.deploy import X  ->  from _winslow_ab12.beta.tasks.deploy import X
        import tasks.deploy         ->  bound to _winslow_ab12.beta.tasks (correct head)
        from ..build import Y       ->  unchanged   # relative (level>0)
        from winslow import Task    ->  unchanged   # no scope owns "winslow"
        import time                 ->  unchanged   # stdlib
    """
    if getattr(builtins.__import__, "_winslow_scoped", False):
        return
    with _scope_lock:
        if getattr(builtins.__import__, "_winslow_scoped", False):
            return  # Another thread won the install race and its hook is active.
        real_import = builtins.__import__

        def _import(name, globals=None, locals=None, fromlist=(), level=0):
            # level 0 is an absolute import. level > 0 is relative and is
            # already scoped.
            if level == 0 and globals:
                pkg = globals.get("__package__") or globals.get("__name__") or ""
                if pkg.startswith(_SCOPE_NS):
                    head = name.split(".", 1)[0]
                    # Walk the package ancestors of the caller, from the nearest.
                    # Route the import into the first ancestor that owns `head`.
                    # That ancestor is the root of this workflow.
                    parts = pkg.split(".")
                    for depth in range(len(parts), 0, -1):
                        ancestor = ".".join(parts[:depth])
                        if _has_submodule(ancestor, head):
                            real_import(
                                f"{ancestor}.{name}", globals, locals, fromlist, level
                            )
                            # `from x import y` needs the leaf module. Bare
                            # `import x.y` binds the head, so return the scoped
                            # head. The leaf would bind the wrong object.
                            if fromlist:
                                return sys.modules[f"{ancestor}.{name}"]
                            return sys.modules[f"{ancestor}.{head}"]
            return real_import(name, globals, locals, fromlist, level)

        _import._winslow_scoped = True
        _import._winslow_real = real_import  # the shadowed importer, for _uninstall
        builtins.__import__ = _import


def _uninstall_scoped_import():
    """Restore the real importer. This function is idempotent. It does nothing if
    another wrapper is installed above this one, because a hook cannot remove
    itself from the middle of an import chain. The chain thus stays intact and
    the outer wrapper is not destroyed. For tests only: in production the hook
    stays installed for the life of the process."""
    hook = builtins.__import__
    if getattr(hook, "_winslow_scoped", False):
        builtins.__import__ = hook._winslow_real


@contextmanager
def isolated_scopes():
    """For tests only. Remove the process-global state that iter_dir_modules
    installs: the import hook, the scoped packages in sys.modules and the
    sys.path entry. A test can thus load a workflow directory and leave no scope
    state for the next test. Do not use this in production, because it removes
    the scoping during a run.

    A multithreaded test is NOT concurrent here. The state that this context
    saves and restores is process-global, so the full context holds _scope_lock,
    and two contexts that overlap run in sequence. For a parallel test use more
    than one process, for example with pytest-xdist. Each worker then has its own
    interpreter state."""
    with _scope_lock:
        modules_before = set(sys.modules)
        path_before = list(sys.path)
        try:
            yield
        finally:
            _uninstall_scoped_import()
            for name in set(sys.modules) - modules_before:
                if name.startswith(_SCOPE_NS):
                    del sys.modules[name]
            sys.path[:] = path_before


def _module_matches(rel_dir, file, only, under):
    """The location filter of a module scan. `only` holds file names, `under`
    holds directory names. A file matches `under` when a component of its
    relative directory is in the set, at any depth."""
    if only is None and under is None:
        return True
    if only is not None and file in only:
        return True
    return under is not None and any(
        part in under for part in rel_dir.split(os.sep) if part not in (".", "")
    )


def iter_dir_module_names(directory, recursive=True, only=None, under=None):
    """Prepare the import scope of the directory and yield the scoped module name
    of each .py file. The caller does the import and sets the error policy.
    `only` and `under` select the modules (see _module_matches)."""
    directory = os.path.abspath(directory)
    if not os.path.isdir(directory):
        raise ValueError(f"'{directory}' is not a directory.")

    # Keep the directory importable by its bare name as a fallback. Workflow code
    # can import a module that is shared at the project root, for example
    # `from dummy_tasks import X`. Two workflows do not clash, because the scoped
    # __import__ resolves the top-level names of a workflow first. It intercepts
    # the import before sys.path is read, so sys.path serves only the names that
    # no workflow owns.
    if directory not in sys.path:
        sys.path.append(directory)

    # Load the directory as a package with a unique name. A hash of the directory
    # prevents a collision in sys.modules when two workflows use the same file
    # names, for example tasks/deploy/staging.py. The scope package has a real
    # __path__, so the usual import machinery resolves the submodules and the
    # relative imports in the scope. A change to sys.path is thus unnecessary.
    # Such a change would leak a global `tasks` that sibling workflows collide
    # on.
    dir_hash = hashlib.md5(directory.encode()).hexdigest()[:8]
    scope_prefix = f"{_SCOPE_NS}{dir_hash}"

    # Register the directory as the root package of the scope, with a real
    # __path__, and install the scoped importer. The usual import machinery then
    # resolves the submodules and the relative imports in the scope, and the
    # importer routes the absolute self-imports there too.
    _ensure_package(scope_prefix, directory)
    _install_scoped_import()

    ignore = (".", "_")
    for root, dirs, files in os.walk(directory):
        if not recursive and root != directory:
            break
        dirs[:] = [d for d in dirs if not d.startswith(ignore)]
        rel_dir = os.path.relpath(root, directory)
        for file in files:
            if file.startswith(ignore) or not file.endswith(".py"):
                continue
            if not _module_matches(rel_dir, file, only, under):
                continue
            rel = os.path.relpath(os.path.join(root, file), directory)
            module_name = rel.replace(os.sep, ".")[:-3]
            yield f"{scope_prefix}.{module_name}"


def iter_dir_modules(directory, recursive=True, only=None, under=None):
    for scoped_name in iter_dir_module_names(directory, recursive, only, under):
        # import_module drives the standard machinery: the scoped parent
        # packages, __package__ and the scoped __import__. It returns a cached
        # module without a change.
        yield importlib.import_module(scoped_name)


def classes_in_module(module, base_class):
    return [
        obj
        for _, obj in inspect.getmembers(module, inspect.isclass)
        if obj.__module__ == module.__name__
        and issubclass(obj, base_class)
        and obj is not base_class
        and not get_is_abstract(obj)
    ]


def execute_in_threads(func, items, max_workers=None):
    """
    Execute a function or a method in parallel with a ThreadPoolExecutor. All
    threads complete their work before this function returns.

    Each call runs in a copy of the context of the caller, so a worker reads
    the same ContextVar values (see winslow.task.context).

    Args:
        func (Callable): The function or the method to execute. It takes one argument.
        items (List): The items to give to the function as arguments.
        max_workers (int, optional): The maximum number of threads. The default is
                                     `None`, and ThreadPoolExecutor then selects
                                     the number.

    Returns:
        None
    """
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                contextvars.copy_context().run,
                func,
                *(item if isinstance(item, tuple) else (item,)),
            )
            for item in items
        ]
        for future in futures:
            future.result()  # Wait, and raise the exception of the task if there is one


SIZE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def human_readable_size(size, decimal_places=2):
    for unit in SIZE_UNITS:
        if size < 1000 or unit == "PB":
            break
        size /= 1000
    return f"{size:.{decimal_places}f} {unit}"
