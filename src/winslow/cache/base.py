import re
import time
import inspect
import threading

from functools import cached_property

import networkx as nx

from winslow.exceptions import CacheReentrancyError, MisconfigurationError
from winslow.logger import LOGGER
from winslow.util import camel_to_snake, to_tuple
from winslow.cache.log import cache_logger, emit_lazy_error
from winslow.cache.storage import MemoryStorage, MISSING, StorageRecord


# The name of a cache is an attribute on a container, so it must be a valid
# Python identifier: stricter than _NAME_PATTERN, which allows a dash.
_CACHE_NAME_PATTERN = re.compile(r"[a-z_][a-z0-9_]*\Z")


class Entry:
    """One cached field of a cache class, declared with @entry. A descriptor
    in the style of TransientProperty (see BaseCache._entry_value)."""

    def __init__(self, func, eager=False, depends_on=(), ttl=None):
        self.func = func
        self.eager = eager
        self.depends_on = to_tuple(depends_on) if depends_on else ()
        self.ttl = ttl
        self.__doc__ = func.__doc__

    def __set_name__(self, owner, name):
        self._attr_name = name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        return obj._entry_value(self)

    def __set__(self, obj, value):
        raise AttributeError(
            f"'{self._attr_name}' is a cache entry and cannot be assigned. "
            f"A fresh value comes from invalidate and a recomputation."
        )

    def is_expired(self, record):
        """True when the record is older than the ttl of this entry."""
        if self.ttl is None:
            return False
        return time.time() - record.written_at >= self.ttl


def entry(func=None, *, eager=False, depends_on=(), ttl=None):
    """Declare a cached field: cached_property plus a lock (see docs/caching.md).
    Treat every value as immutable: a fresh value comes from a recomputation."""
    if func is not None:
        if eager or depends_on or ttl is not None:
            raise MisconfigurationError(
                "entry(func, ...) drops the options - decorate with "
                "@entry(eager=..., depends_on=..., ttl=...) so they apply."
            )
        return Entry(func)

    def wrap(f):
        return Entry(f, eager=eager, depends_on=depends_on, ttl=ttl)

    return wrap


def declared_entries(klass):
    """The Entry descriptors in the MRO of a class, as {name: Entry}. The most
    derived definition of a name decides (compare _DeclarationMeta._collect)."""
    members = {}
    for kls in reversed(klass.__mro__):
        members.update(vars(kls))
    return {name: val for name, val in members.items() if isinstance(val, Entry)}


def _dependency_graph(entries):
    """A directed graph over the entry names. An edge points from a dependency
    to its dependent, so a topological sort orders upstream first."""
    graph = nx.DiGraph()
    graph.add_nodes_from(entries)
    graph.add_edges_from(
        (dep, name) for name, e in entries.items() for dep in e.depends_on
    )
    return graph


def eager_fields(klass):
    """The eager entry names, dependencies first. The order matters only for a
    serial population; a parallel one resolves through the field locks."""
    eager = {name: e for name, e in declared_entries(klass).items() if e.eager}
    return list(nx.lexicographical_topological_sort(_dependency_graph(eager)))


class BaseCache:
    """The base of the declarative caches: the registries discover the
    concrete subclasses and validate them at collection (see docs/caching.md)."""

    class Meta:
        abstract = True

    # The attribute name on the container (see get_name).
    name = None

    # The backend for the values of one instance, in line with graph_class and
    # registry_class on Workflow.
    storage_class = MemoryStorage

    def __init__(self):
        self._entries = declared_entries(type(self))
        self._storage = self.storage_class(self.get_name(), self._storage_namespace())
        # One lock per field, so two threads that hit a cold field compute it
        # once. Vanilla cached_property has no lock since Python 3.12.
        self._locks = {name: threading.Lock() for name in self._entries}
        # The fields a thread is computing right now, per thread. _entry_value
        # reads it to turn an undeclared read cycle into a loud error.
        self._reading = threading.local()

    def __str__(self):
        return self.get_name()

    @classmethod
    def get_name(cls):
        name = cls.name if cls.name is not None else camel_to_snake(cls.__name__)
        if not isinstance(name, str) or not name:
            raise MisconfigurationError(
                f"Invalid name definition for {cls} ({name!r}) - needs to be a non-empty string."
            )
        if not _CACHE_NAME_PATTERN.match(name):
            raise MisconfigurationError(
                f"Invalid cache name '{name}' for {cls}: must match [a-z_][a-z0-9_]*, "
                f"because the name is an attribute on the cache container."
            )
        return name

    @property
    def logger(self):
        """The logger for a loader emission, resolved per access from the
        ambient context - never stored, because a GlobalCache outlives every
        session (see cache_logger)."""
        return cache_logger()

    def _storage_namespace(self):
        """The key prefix that separates the scopes and the workflows in a
        persistent backend (see JsonFileStorage). The scope bases define it."""
        raise NotImplementedError

    def _entry_value(self, entry):
        """Return the value of one entry. A cold or expired entry computes
        under its field lock and writes through the storage layer."""
        name = entry._attr_name
        chain = getattr(self._reading, "chain", ())
        if name in chain:
            # The thread already holds the lock of this field, so a second
            # acquisition would deadlock it silently and forever.
            cycle = " -> ".join((*chain[chain.index(name) :], name))
            raise CacheReentrancyError(
                f"Cache '{self}': the loader of '{chain[-1]}' reads '{name}' "
                f"while '{name}' is computing on the same thread ({cycle}) - "
                f"an undeclared read cycle."
            )
        with self._locks[name]:
            record = self._storage.read(name)
            if record is not MISSING:
                if not entry.is_expired(record):
                    return record.value
                cache_logger().info(
                    f"Cache '{self}': entry '{name}' went stale "
                    f"(age {time.time() - record.written_at:.1f}s, ttl {entry.ttl}s) - recomputing."
                )
            self._reading.chain = (*chain, name)
            try:
                value = entry.func(self)
            except Exception as exc:
                # Only the outermost frame emits: a nested read raises again
                # through its caller, and each error reaches the backends once.
                if not chain:
                    emit_lazy_error(self, name, exc)
                raise
            finally:
                self._reading.chain = chain
            # Serve what the storage stored: a serializing backend returns the
            # normalized round trip, so the shape never changes after a restart.
            stored = self._storage.write(
                name, StorageRecord(value=value, written_at=time.time())
            )
            return stored.value

    def invalidate(self, *names):
        """Drop the entries and their declared dependents, transitively. The
        next access recomputes each dropped entry, exactly like a ttl expiry."""
        if not names:
            raise TypeError(
                f"Cache '{self}': invalidate() takes at least one entry name - "
                f"to drop every entry, call invalidate_all()."
            )
        if unknown := [n for n in names if n not in self._entries]:
            raise AttributeError(
                f"Cache '{self}' has no entry {', '.join(repr(n) for n in unknown)} - "
                f"known entries: {sorted(self._entries)}"
            )
        graph = _dependency_graph(self._entries)
        affected = set(names).union(*(nx.descendants(graph, n) for n in names))
        order = [n for n in nx.topological_sort(graph) if n in affected]
        self._drop_entries(order, trigger=names)

    def invalidate_all(self):
        """Drop every entry of the instance."""
        order = list(nx.topological_sort(_dependency_graph(self._entries)))
        self._drop_entries(order, trigger=None)

    def _drop_entries(self, names, trigger):
        """Drop upstream first: the other order lets a reader recompute a
        dependent from the stale upstream and keep it."""
        chain = getattr(self._reading, "chain", ())
        if blocked := [name for name in names if name in chain]:
            # The thread holds the locks of its own chain, so the drop would
            # deadlock silently and forever (compare _entry_value).
            raise CacheReentrancyError(
                f"Cache '{self}': an invalidation from the loader of "
                f"'{chain[-1]}' reaches {', '.join(repr(n) for n in blocked)}, "
                f"which is computing on the same thread."
            )
        dropped = [name for name in names if self._drop_entry(name)]
        if not dropped:
            return
        label = (
            f"invalidate({', '.join(repr(n) for n in trigger)})"
            if trigger is not None
            else "invalidate_all()"
        )
        cache_logger().info(
            f"Cache '{self}': {label} dropped {', '.join(repr(n) for n in dropped)}."
        )

    def _drop_entry(self, name):
        """Drop one entry and report whether a live value was present. One lock
        at a time: two held locks could deadlock against a nested computation."""
        with self._locks[name]:
            record = self._storage.read(name)
            self._storage.delete(name)
            return record is not MISSING and not self._entries[name].is_expired(record)


class GlobalCache(BaseCache):
    """A cache with process scope, shared by the workflows. The instances live
    for the process (see winslow.cache.get_global_cache)."""

    class Meta:
        abstract = True

    def __init__(self, orchestrator_config):
        super().__init__()
        self.orchestrator_config = orchestrator_config

    def _storage_namespace(self):
        return "global"


class WorkflowCache(BaseCache):
    """A cache with session scope. A new session builds fresh instances, and
    the container is dropped when the session ends."""

    class Meta:
        abstract = True

    def __init__(self, workflow_config):
        # Before super(), which builds the storage from the namespace.
        self.workflow_config = workflow_config
        super().__init__()

    def _storage_namespace(self):
        # The workflow stamps its identity (see Workflow.cache_namespace). The
        # workflows/ segment keeps any stamp out of the global scope.
        namespace = getattr(self.workflow_config, "cache_namespace", None)
        if namespace is None:
            LOGGER.warning(
                f"{type(self).__name__} was built outside a workflow - its "
                f"storage shares the 'workflows/_unscoped' namespace."
            )
            namespace = "_unscoped"
        return f"workflows/{namespace}"


# The instance attributes of the scope bases. A scan of vars(kls) cannot see
# them, so _reserved_names lists them explicitly.
_INSTANCE_ATTRIBUTE_NAMES = frozenset({"workflow_config", "orchestrator_config"})


def _reserved_names(cache_class):
    """The public non-entry names in the MRO. An entry must not shadow these,
    so the cache API and the project helpers stay reachable on every cache."""
    return _INSTANCE_ATTRIBUTE_NAMES | {
        name
        for kls in cache_class.__mro__
        for name, member in vars(kls).items()
        if not name.startswith("_") and not isinstance(member, Entry)
    }


def validate_cache_class(cache_class):
    """Validate the declarations of one cache class at collection, so a bad
    declaration fails early and names the class and the field."""
    entries = declared_entries(cache_class)
    label = cache_class.__name__

    _validate_members(label, cache_class)
    reserved = _reserved_names(cache_class)
    for name, e in entries.items():
        _validate_entry(label, name, e, entries, reserved)
    _validate_acyclic(label, entries)


def _validate_members(label, cache_class):
    """Reject the one-spelling violations: an underscore entry and a plain
    cached_property."""
    members = (
        (name, member)
        for kls in cache_class.__mro__
        for name, member in vars(kls).items()
    )
    for name, member in members:
        if isinstance(member, Entry) and name.startswith("_"):
            raise MisconfigurationError(
                f"{label}: entry '{name}' must not start with an underscore."
            )
        if isinstance(member, cached_property):
            raise MisconfigurationError(
                f"{label}: '{name}' is a functools.cached_property. Declare it "
                f"with @entry - one spelling, no silent thread-safety trap."
            )


def _validate_entry(label, name, e, entries, reserved):
    """One entry: its name, its loader signature, its ttl and its dependencies."""
    if name in reserved:
        raise MisconfigurationError(
            f"{label}: entry '{name}' shadows a non-entry member of the cache "
            f"hierarchy - choose another name."
        )
    parameters = list(inspect.signature(e.func).parameters.values())
    if len(parameters) != 1 or any(
        p.kind not in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) for p in parameters
    ):
        raise MisconfigurationError(
            f"{label}.{name}: an @entry function must take exactly 'self'."
        )
    if e.ttl is not None and (
        isinstance(e.ttl, bool) or not isinstance(e.ttl, (int, float)) or e.ttl <= 0
    ):
        raise MisconfigurationError(
            f"{label}.{name}: ttl must be a positive number, got {e.ttl!r}."
        )
    for dep in e.depends_on:
        if dep not in entries:
            raise MisconfigurationError(
                f"{label}.{name}: depends_on {dep!r} is not an @entry of this "
                f"class - known entries: {sorted(entries)}. A dependency "
                f"across cache classes is not supported."
            )
        if e.eager and not entries[dep].eager:
            raise MisconfigurationError(
                f"{label}.{name}: an eager entry cannot depend on the lazy "
                f"entry {dep!r} - the population would load {dep!r} anyway, "
                f"so declare it eager."
            )
        _warn_ttl_mismatch(label, name, e, dep, entries[dep])


def _validate_acyclic(label, entries):
    for cycle in nx.simple_cycles(_dependency_graph(entries)):
        chain = " -> ".join(cycle + [cycle[0]])
        raise MisconfigurationError(f"{label}: cyclical entry dependency: {chain}.")


def _warn_ttl_mismatch(label, name, entry, dep_name, dep_entry):
    """Warn when a dependency expires sooner than its dependent, which can then
    hold stale upstream data. Not an error: it can be an intended trade-off."""
    if dep_entry.ttl is None:
        return
    if entry.ttl is None or entry.ttl > dep_entry.ttl:
        LOGGER.warning(
            f"{label}.{name} (ttl={entry.ttl}) outlives its dependency "
            f"'{dep_name}' (ttl={dep_entry.ttl}) - it can hold data from a "
            f"stale upstream."
        )
