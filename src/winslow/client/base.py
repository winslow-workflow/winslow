"""The shared surface of the session port, defined once. A transport module
implements both classes with these signatures (see winslow.client). Every
method takes values and returns values (see winslow.model).

Each read is one Read declaration and each action one Action declaration on
the contract class. A transport either overrides a read with a method (see
winslow.client.local) or implements read(), which serves every declaration,
and implements submit() for the actions (see winslow.client.websocket). A
declaration also names the wire envelope of the call, so the serve endpoints
dispatch from the same declaration (see PORT_READS, PORT_ACTIONS), and the
permission the caller needs, so the port guards an override (see _PortMeta).

A read the server or the session refuses raises RequestError with the
served reason (see winslow.exceptions). A refused action answers an ack
(see submit). A connection outage of the wire transport raises
ConnectionError or TimeoutError: the request reached no server, so there
is no served reason (see winslow.client.websocket)."""

from dataclasses import field, make_dataclass
from functools import cached_property, partial

from winslow._meta import _DeclarationMeta
from winslow.access.authz import filter_rows, guard
from winslow.access.permissions import Permission
from winslow.actions import Ack, BatchAck, SessionAck
from winslow.model import (
    CacheInfo,
    CacheValueView,
    Descriptors,
    BatchOutcome,
    HostStats,
    ManifestInfo,
    RecordDetail,
    SessionParams,
    SessionInfo,
    SessionSnapshot,
    TaskInfo,
)

REQUIRED = object()


class Call:
    """One call of the port, declared on a contract class: a Read or an
    Action. Each keyword field names one argument in call order: a type, or a
    (type, default) pair. requires names the permissions the caller needs on
    the scope of the port, one of them (see winslow.access.authz)."""

    # The transport method a bound declaration calls (see __get__).
    transport = None

    def __init__(self, *, doc="", requires=None, **fields):
        self.fields = {
            name: spec if isinstance(spec, tuple) else (spec, REQUIRED)
            for name, spec in fields.items()
        }
        self.__doc__ = doc
        self.requires = (requires,) if isinstance(requires, Permission) else requires
        self.name = None
        self.scope = None

    def __set_name__(self, owner, name):
        # The contract class names the declaration. A class that reuses the
        # declaration keeps that name and scope.
        if self.name is None:
            self.name = name
            self.scope = owner.scope

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        return partial(getattr(instance, self.transport), self)

    def __repr__(self):
        return f"{type(self).__name__}({self.scope}.{self.name})"

    @property
    def title(self):
        return "".join(part.title() for part in self.name.split("_"))

    def declared_fields(self):
        """The dataclass fields of the declaration, for a generated dataclass."""
        return [
            (name, kind)
            if default is REQUIRED
            else (name, kind, field(default=default))
            for name, (kind, default) in self.fields.items()
        ]

    def bind(self, args, kwargs):
        """The wire fields of one call: the positional values in declaration
        order, then the keywords, then the declared defaults."""
        if len(args) > len(self.fields):
            raise TypeError(
                f"{self.name}() takes {len(self.fields)} arguments, "
                f"{list(self.fields)}, and got {len(args)} positional."
            )
        bound = dict(zip(self.fields, args))
        twice = sorted(set(bound) & set(kwargs))
        if twice:
            raise TypeError(f"{self.name}() got {twice} both positional and by name.")
        bound.update(kwargs)
        for name, (_, default) in self.fields.items():
            if name not in bound:
                if default is REQUIRED:
                    raise TypeError(f"{self.name}() misses the argument {name!r}.")
                bound[name] = default
        return bound


class Read(Call):
    """One read of the port. result is the value shape (a DTO class, or list,
    tuple, dict) and many marks a tuple of DTOs.

    Exactly one of these names the permission (see winslow.access.authz).
    requires: a refusal raises. visible: the rows filter to the scopes the
    caller holds the permission on, rows names the tuple field of a result
    object. open: any authenticated caller."""

    transport = "read"

    def __init__(
        self, *, result=None, many=False, visible=None, rows=None, open=False, **fields
    ):
        super().__init__(**fields)
        self.result = result
        self.many = many
        self.visible = visible
        self.rows = rows
        self.open = open
        if sum(1 for d in (self.requires, self.visible, self.open) if d) != 1:
            raise TypeError("a Read names exactly one of requires, visible or open.")

    def wrap(self, method):
        """The transport override of this read behind its declared check."""
        if self.requires:
            return guard(method, self.requires)
        if self.visible:
            return filter_rows(method, self.visible, self.rows)
        return method

    @cached_property
    def envelope(self):
        """The request frame of this read: a RequestFrame with the session id
        of a session read and the declared fields. The client constructs it
        and the serve endpoint validates it (see Connection.decode)."""
        # The frames need pydantic, which only the serve and connect extras install.
        from winslow.protocol.frames import RequestFrame

        head = [("kind", str, field(default=self.name))]
        if self.scope == "session":
            head.append(("session_id", str))
        return make_dataclass(
            f"{self.title}Request",
            head + self.declared_fields(),
            bases=(RequestFrame,),
            frozen=True,
            kw_only=True,
        )


class Action(Call):
    """One action of the port. ack is the answer shape, requires the
    permissions the handler checks (see ActionHandler.submit and
    AppActionHandler.submit). counted names the tuple field whose size the
    audit line carries. Called on the contract class, the declaration builds
    the value of one submit. Called on a client, it submits."""

    transport = "submit"

    def __init__(self, *, ack=Ack, counted=None, **fields):
        super().__init__(**fields)
        if not self.requires:
            raise TypeError("an Action names its permissions in requires.")
        self.ack = ack
        self.counted = counted

    def __call__(self, *args, **kwargs):
        """The value of one submit, from the arguments in call order."""
        return self._value_class(**self.bind(args, kwargs))

    @cached_property
    def _value_class(self):
        """A frozen dataclass of the declared fields that carries its
        declaration as spec. The handler reads the fields, the dispatcher
        the spec (see ActionHandler.submit)."""
        # A property, so the declaration does not bind as a descriptor. The
        # codec refuses a field the declaration does not name (see build).
        return make_dataclass(
            f"{self.title}Action",
            self.declared_fields(),
            namespace={
                "spec": property(lambda action, spec=self: spec),
                "__pydantic_config__": {"extra": "forbid"},
            },
            frozen=True,
            kw_only=True,
        )

    def build(self, payload):
        """The value of one wire payload, validated (see ActionFrame)."""
        # The codec needs pydantic, which only the serve and connect extras install.
        from winslow.protocol.codec import CODEC, ValidationError, report

        try:
            return CODEC.decode(self._value_class, payload or {})
        except ValidationError as exc:
            raise ValueError(
                f"bad fields for {self.name} - {report(exc)}. The fields of "
                f"{self.name} are {list(self.fields)}."
            ) from None

    def refused(self, reason):
        return self.ack(accepted=False, reason=reason)

    def count(self, action):
        """The size the audit line carries, or None."""
        return None if self.counted is None else len(getattr(action, self.counted))


class _PortMeta(_DeclarationMeta):
    """Collects the Read and Action declarations, and puts the check each read
    declares behind the method a transport overrides it with (see Read.wrap)."""

    def __new__(cls, name, bases, dct):
        result = super().__new__(cls, name, bases, dct)
        result.read_meta = cls._collect(bases, dct, Read, "read_meta")
        result.action_meta = cls._collect(bases, dct, Action, "action_meta")
        for attr, spec in result.read_meta.items():
            member = dct.get(attr)
            if member is not None and not isinstance(member, Read):
                setattr(result, attr, spec.wrap(member))
        return result


class Port(metaclass=_PortMeta):
    """A contract class of the port. scope names the endpoint the reads serve
    from. read_meta and action_meta hold every declaration through the MRO."""

    scope = None

    def submit(self, spec, *args, **kwargs):
        """Serve one Action declaration: the bound fields reach the handler
        of the port, and its ack comes back. A refused action answers an ack
        that carries the reason (see ActionHandler, AppActionHandler)."""
        raise NotImplementedError(
            f"{type(self).__name__} serves no {spec.name} - implement submit() "
            f"for every Action declaration."
        )

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
        visible=Permission.VIEW_TASK,
        doc="Return a SessionInfo per live session.",
    )
    descriptors = Read(
        result=Descriptors,
        visible=Permission.START_WORKFLOW,
        rows="workflows",
        doc="Return the Descriptors of the process: the collected workflows "
        "with their form options, plus the orchestrator overrides.",
    )
    manifests = Read(
        result=ManifestInfo,
        many=True,
        visible=Permission.START_WORKFLOW,
        doc="Return a ManifestInfo per restorable session: the open manifests "
        "that name no live session.",
    )
    host_stats = Read(
        result=HostStats,
        open=True,
        doc="Return the HostStats of the process: memory, cpu load, hostname "
        "and user of the host that runs the sessions.",
    )
    create_session = Action(
        requires=Permission.START_WORKFLOW,
        workflow=str,
        overrides=(dict | None, None),
        values=(dict | None, None),
        ack=SessionAck,
        doc="Build, initialize, persist and register one session of the "
        "workflow. The ack carries its SessionInfo. An absent override or "
        "value takes the default of the workflow. A failure of the workflow "
        "init refuses, with the traceback in detail.",
    )
    restore_session = Action(
        requires=Permission.START_WORKFLOW,
        session_id=str,
        ack=SessionAck,
        doc="Re-create the session of an open manifest under its stored id, "
        "seeded from state. The ack carries its SessionInfo.",
    )

    def environment(self):
        """Return the EnvInfo of the process that runs the sessions. The wire
        transport takes it from the hello_ok frame, so the call needs no
        request (see HelloOkFrame)."""
        raise NotImplementedError

    def permissions(self):
        """Return the PermissionContext of this client. The in-process client
        holds every permission. The wire transport reads the hello_ok frame,
        and holds no scope before the hello and between a drop and the next
        hello, so a UI renders its controls disabled (see winslow.access.permissions)."""
        raise NotImplementedError

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
        requires=Permission.VIEW_TASK,
        result=SessionSnapshot,
        doc="Return the SessionSnapshot: statuses, batch rows, session log "
        "backlog, session meta.",
    )
    roster = Read(
        requires=Permission.VIEW_TASK,
        result=TaskInfo,
        many=True,
        doc="Return a stub TaskInfo per task, in the order of the launch filter.",
    )
    task_detail = Read(
        requires=Permission.VIEW_TASK,
        task_key=str,
        result=TaskInfo,
        doc="Return the full TaskInfo of one task, evaluated and with the "
        "trust fields filled (see Workflow.task_info).",
    )
    record_detail = Read(
        requires=Permission.VIEW_TASK,
        batch_uuid=str,
        task_key=str,
        result=RecordDetail,
        doc="Return the RecordDetail of one execution record.",
    )
    history = Read(
        requires=Permission.VIEW_TASK,
        result=BatchOutcome,
        many=True,
        doc="Return a BatchOutcome per batch, with the per-task outcomes.",
    )
    log_tail = Read(
        requires=Permission.VIEW_TASK,
        batch_uuid=str,
        task_key=str,
        limit=(int | None, None),
        result=list,
        doc="Return the last `limit` stored log lines of one record. None "
        "reads the default window of the record store.",
    )
    caches = Read(
        requires=Permission.VIEW_TASK,
        result=CacheInfo,
        many=True,
        doc="Return a CacheInfo per cache of the session.",
    )
    cache_value = Read(
        requires=Permission.VIEW_TASK,
        cache_name=str,
        entry_name=str,
        result=CacheValueView,
        doc="Return the CacheValueView of one entry: the rendered value, its "
        "encoding, and the error context.",
    )
    apply_filter = Read(
        requires=Permission.VIEW_TASK,
        query=str,
        scope=(str, "tasks"),
        result=tuple,
        doc="Return the identity keys the query matches over the named corpus, "
        "'tasks' or 'history' (see Workflow.filter_keys).",
    )
    batch_options = Read(
        requires=Permission.VIEW_TASK,
        result=dict,
        doc="Return the baseline batch option values of the session as a dict. "
        "A client prefills its toggles from it and sends its own values with "
        "every submit (see run_tasks).",
    )
    session_params = Read(
        requires=Permission.VIEW_TASK,
        result=SessionParams,
        doc="Return the SessionParams: settings_snapshot plus the resolved "
        "workflow_config values.",
    )

    # --- actions ----------------------------------------------------------------

    run_tasks = Action(
        requires=Permission.RUN_TASK,
        keys=tuple,
        options=(dict | None, None),
        ack=BatchAck,
        counted="keys",
        doc="Submit a run batch over the identity keys. options carries the "
        "batch options of this submit as {name: bool}, over the baseline of "
        "the session (see batch_options).",
    )
    check_tasks = Action(
        requires=Permission.CHECK_TASK,
        keys=tuple,
        options=(dict | None, None),
        ack=BatchAck,
        counted="keys",
        doc="Submit a check batch over the identity keys. options works as in "
        "run_tasks.",
    )
    stop_batch = Action(
        # Either permission: the handler refuses a run batch without run_task.
        requires=(Permission.CHECK_TASK, Permission.RUN_TASK),
        batch_uuid=str,
        doc="Request the stop of one live batch. The ack accepts the request, "
        "and the batch drains on its worker threads afterwards.",
    )
    end_session = Action(
        # Either permission: the handler refuses an unsettled session without
        # end_workflow.
        requires=(Permission.END_WORKFLOW, Permission.CHECK_TASK),
        force=(bool, False),
        doc="End the session. force stops its running batches first.",
    )
    load_cache_entries = Action(
        requires=Permission.RUN_TASK,
        entries=tuple,
        counted="entries",
        doc="Load the cache entries, each a (cache_name, entry_name) pair. "
        "A single selection sends a list of one pair.",
    )
    clear_cache_entries = Action(
        requires=Permission.RUN_TASK,
        entries=tuple,
        counted="entries",
        doc="Invalidate the cache entries. entries works as in load_cache_entries.",
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


# The surface of the port, keyed by wire name: every Read and every Action
# declaration, so the endpoints serve exactly what the local TUI consumes. A
# request frame names the read under "kind", an action frame the action under
# "action" (see Connection.envelope_class, Connection.run_action).
PORT_READS = {**AppClient.read_meta, **SessionClient.read_meta}
PORT_ACTIONS = {**AppClient.action_meta, **SessionClient.action_meta}
