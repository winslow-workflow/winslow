"""The shared surface of the session port, defined once. A transport module
implements both classes with these signatures (see winslow.client). Every
method takes values and returns values (see winslow.model).

Each read is one Read declaration on the contract class. A transport either
overrides the read with a method (see winslow.client.local) or implements
read(), which serves every declaration (see winslow.client.websocket). The
declaration also names the wire envelope of the read, so the serve doors
dispatch from the same declaration (see PORT_READS).

A read the server or the session refuses raises RequestError with the
served reason (see winslow.exceptions). A refused action answers an ack
(see submit). A connection outage of the wire transport raises
ConnectionError or TimeoutError: the request reached no server, so there
is no served reason (see winslow.client.websocket)."""

from dataclasses import field, make_dataclass
from functools import cached_property, partial

from winslow._meta import _DeclarationMeta
from winslow.protocol.frames import RequestFrame
from winslow.model import (
    CacheInfo,
    CacheValueView,
    Descriptors,
    BatchOutcome,
    ManifestInfo,
    RecordDetail,
    SessionParams,
    SessionInfo,
    SessionSnapshot,
    TaskInfo,
)

REQUIRED = object()


class Read:
    """One read of the port, declared on a contract class. Each keyword field
    names one argument in call order: a type, or a (type, default) pair. result
    is the value shape (a DTO class, or list, tuple, dict) and many marks a
    tuple of DTOs. traceback marks a read that runs project code: its refusal
    carries the traceback."""

    def __init__(self, *, result=None, many=False, traceback=False, doc="", **fields):
        self.fields = {
            name: spec if isinstance(spec, tuple) else (spec, REQUIRED)
            for name, spec in fields.items()
        }
        self.result = result
        self.many = many
        self.traceback = traceback
        self.__doc__ = doc
        self.name = None
        self.scope = None

    def __set_name__(self, owner, name):
        self.name = name
        self.scope = owner.scope

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        return partial(instance.read, self)

    def __repr__(self):
        return f"Read({self.scope}.{self.name})"

    @cached_property
    def envelope(self):
        """The request frame of this read: a RequestFrame with the session id
        of a session read and the declared fields. The client constructs it
        and the serve door validates it (see Connection.decode)."""
        head = [("kind", str, field(default=self.name))]
        if self.scope == "session":
            head.append(("session_id", str))
        declared = [
            (name, kind)
            if default is REQUIRED
            else (name, kind, field(default=default))
            for name, (kind, default) in self.fields.items()
        ]
        title = "".join(part.title() for part in self.name.split("_"))
        return make_dataclass(
            f"{title}Request",
            head + declared,
            bases=(RequestFrame,),
            frozen=True,
            kw_only=True,
        )

    def bind(self, args, kwargs):
        """The wire fields of one call: the positional values in declaration
        order, then the keywords, then the declared defaults."""
        bound = dict(zip(self.fields, args))
        bound.update(kwargs)
        for name, (_, default) in self.fields.items():
            if name not in bound:
                if default is REQUIRED:
                    raise TypeError(f"{self.name}() misses the argument {name!r}.")
                bound[name] = default
        return bound


class _PortMeta(_DeclarationMeta):
    def __new__(cls, name, bases, dct):
        result = super().__new__(cls, name, bases, dct)
        result.read_meta = cls._collect(bases, dct, Read, "read_meta")
        return result


class Port(metaclass=_PortMeta):
    """A contract class of the port. scope names the door the reads serve
    from. read_meta holds every Read declaration through the MRO."""

    scope = None

    def read(self, spec, *args, **kwargs):
        """Serve one Read declaration. A transport that does not override
        every read as a method implements this instead."""
        raise NotImplementedError(
            f"{type(self).__name__} serves no {spec.name} - override the "
            f"method, or implement read() for every declaration."
        )


class AppClient(Port):
    """The dashboard scope: the live sessions, descriptors, open manifests,
    create and restore. It hands out SessionClients (see session)."""

    scope = "app"

    sessions = Read(
        result=SessionInfo,
        many=True,
        doc="Return a SessionInfo per live session.",
    )
    descriptors = Read(
        result=Descriptors,
        doc="Return the Descriptors of the process: the collected workflows "
        "with their form options, plus the orchestrator overrides.",
    )
    manifests = Read(
        result=ManifestInfo,
        many=True,
        doc="Return a ManifestInfo per restorable session: the open manifests "
        "that name no live session.",
    )
    create_session = Read(
        workflow=str,
        overrides=(dict | None, None),
        values=(dict | None, None),
        result=SessionInfo,
        traceback=True,
        doc="Build, initialize, persist and register one session. Return its "
        "SessionInfo. overrides and values default to {} at the handler, so "
        "None and an absent field behave the same.",
    )
    restore_session = Read(
        session_id=str,
        result=SessionInfo,
        traceback=True,
        doc="Re-create the session of an open manifest under its stored id, "
        "seeded from state. Return its SessionInfo.",
    )

    def session(self, session_id):
        """Return the SessionClient of one live session. The local transport
        builds a fresh client per call. The wire transport returns the one
        shared lane of the session."""
        raise NotImplementedError

    def subscribe_connection(self, handler):
        """Connect the handler to the ConnectionEvent lane (see
        winslow.model). The wire transport emits on a drop and on the
        reconnect. The in-process transport keeps the handler idle. A
        handler can run on any thread."""
        raise NotImplementedError


class SessionClient(Port):
    """One session: the reads, the subscriptions, and the actions."""

    scope = "session"

    # --- reads ---------------------------------------------------------------

    snapshot = Read(
        result=SessionSnapshot,
        doc="Return the SessionSnapshot: statuses, batch rows, session log "
        "backlog, session meta.",
    )
    roster = Read(
        result=TaskInfo,
        many=True,
        doc="Return a stub TaskInfo per task, in the order of the launch filter.",
    )
    task_detail = Read(
        task_key=str,
        result=TaskInfo,
        doc="Return the full TaskInfo of one task, evaluated and with the "
        "trust fields filled (see Workflow.task_info).",
    )
    record_detail = Read(
        batch_uuid=str,
        task_key=str,
        result=RecordDetail,
        doc="Return the RecordDetail of one execution record.",
    )
    history = Read(
        result=BatchOutcome,
        many=True,
        doc="Return a BatchOutcome per batch, with the per-task outcomes.",
    )
    log_tail = Read(
        batch_uuid=str,
        task_key=str,
        limit=(int | None, None),
        result=list,
        doc="Return the last `limit` stored log lines of one record. None "
        "reads the default window of the record store.",
    )
    caches = Read(
        result=CacheInfo,
        many=True,
        doc="Return a CacheInfo per cache of the session.",
    )
    cache_value = Read(
        cache_name=str,
        entry_name=str,
        result=CacheValueView,
        doc="Return the CacheValueView of one entry: the rendered value, its "
        "encoding, and the error context.",
    )
    apply_filter = Read(
        query=str,
        scope=(str, "tasks"),
        result=tuple,
        doc="Return the identity keys the query matches over the named corpus, "
        "'tasks' or 'history' (see Workflow.filter_keys). A bad query raises "
        "RequestError with the parse error.",
    )
    batch_options = Read(
        result=dict,
        doc="Return the baseline batch option values of the session as a dict. "
        "A fresh client prefills its toggles from it. Each submit then carries "
        "the values of the client (see RunTasks.options).",
    )
    session_params = Read(
        result=SessionParams,
        doc="Return the SessionParams: settings_snapshot plus the resolved "
        "workflow_config values.",
    )

    # --- subscriptions ---------------------------------------------------------

    def subscribe(self, topic, handler):
        """Connect the handler to one event topic of the session. The topics
        are the session bus event classes (see winslow.events) plus
        CacheUpdatedEvent and SessionLogEvent (see winslow.model). A handler
        can run on any thread. The dispatch order between handlers is
        undefined."""
        raise NotImplementedError

    def unsubscribe(self, topic, handler):
        """Disconnect the handler (see subscribe). An unknown pair is a
        no-op, so a teardown path can run twice."""
        raise NotImplementedError

    def subscribe_task_log(self, task_key, handler):
        """Connect the handler to the live log stream of one task, outside
        any batch, and return the buffered backlog lines. The handler
        receives TaskLogEvent values."""
        raise NotImplementedError

    def unsubscribe_task_log(self, task_key, handler):
        """Disconnect the task log handler (see subscribe_task_log)."""
        raise NotImplementedError

    # --- actions ----------------------------------------------------------------

    def submit(self, action):
        """Submit one action dataclass (see winslow.actions) and return its
        ack. A refused action answers an ack that carries the reason."""
        raise NotImplementedError


# The read surface of the port, keyed by request kind: every Read declaration,
# so the doors serve exactly what the local TUI consumes. A request frame names
# the read under "kind" (see Connection.envelope_class).
PORT_READS = {**AppClient.read_meta, **SessionClient.read_meta}
