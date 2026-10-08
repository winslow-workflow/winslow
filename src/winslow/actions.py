"""The inbound action path of a session. A presentation layer translates
user input into one action dataclass and submits it to the ActionHandler of
the session. Action fields are values only: identity keys, scalars (the
payload rule, see winslow.events)."""

import contextvars
import threading
from dataclasses import asdict, dataclass, fields

from winslow._meta import _tagged, handles
from winslow.cache import declared_entries
from winslow import authz
from winslow.authz import requires
from winslow.permissions import Permission
from winslow.runner.execution import ExecutionAction
from winslow.exceptions import SessionEndingError
from winslow.task.context import BatchOptions
from winslow.util import execute_in_threads


@dataclass(frozen=True)
class Ack:
    """The synchronous answer to an action: accepted or refused. The reason
    names what refused the action."""

    accepted: bool
    reason: str | None = None


@dataclass(frozen=True)
class BatchAck(Ack):
    """The answer to a batch submit. An accepted submit carries the uuid of
    the created batch; the batch worker threads do the work."""

    batch_uuid: str | None = None


class Action:
    """The base of every action. name is the wire name (see ActionFrame). The
    actions register by name at class creation (see build). ack_class is the
    answer the action gets, and audit_keys the size the audit line carries."""

    name = None
    by_name = {}
    ack_class = Ack

    @property
    def audit_keys(self):
        return None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # A base with no name is an action kind and stays unregistered (see Lane).
        if cls.name is not None:
            Action.by_name[cls.name] = cls

    @classmethod
    def build(cls, name, payload):
        """The action instance of one wire name and its field dict (see
        ActionFrame). The codec validates the payload against the dataclass."""
        # The codec needs pydantic, which only the serve and connect extras install.
        from winslow.protocol.codec import CODEC, ValidationError, report

        action_class = cls.by_name.get(name)
        if action_class is None:
            raise ValueError(
                f"{name!r} names no action. The actions are {sorted(cls.by_name)}."
            )
        try:
            return CODEC.decode(action_class, payload or {})
        except ValidationError as exc:
            raise ValueError(
                f"bad fields for {name} - {report(exc)}. The fields of {name} are "
                f"{[f.name for f in fields(action_class)]}."
            ) from None


class BatchAction(Action):
    """An action that submits a batch over keys and answers with a BatchAck."""

    ack_class = BatchAck

    @property
    def audit_keys(self):
        return len(self.keys)


@dataclass(frozen=True)
class RunTasks(BatchAction):
    """options carries the batch options of this submit: {name: bool},
    over the session baseline. They are client view state, so each client
    sends its own with every batch (see BatchOptions)."""

    name = "run_tasks"
    keys: tuple
    options: dict | None = None


@dataclass(frozen=True)
class CheckTasks(BatchAction):
    """The check form of RunTasks; options works the same."""

    name = "check_tasks"
    keys: tuple
    options: dict | None = None


@dataclass(frozen=True)
class StopBatch(Action):
    name = "stop_batch"
    batch_uuid: str


@dataclass(frozen=True)
class EndSession(Action):
    name = "end_session"
    force: bool = False


@dataclass(frozen=True)
class LoadCacheEntries(Action):
    """Bulk-only, like RunTasks: a single selection sends a one-pair list.
    Each pair is (cache_name, entry_name)."""

    name = "load_cache_entries"
    entries: tuple


@dataclass(frozen=True)
class ClearCacheEntries(Action):
    """Bulk-only, like LoadCacheEntries. "Clear" is the action verb on the
    wire; the handler calls cache.invalidate internally."""

    name = "clear_cache_entries"
    entries: tuple


class ActionHandler:
    """One per session: the inbound half of the session boundary (the bus is
    the outbound half, see SessionBus). The handler accepts one action,
    resolves its values to live objects, gates it, delegates it to the runner
    or the session, and answers with an ack. It refuses with an ack, never
    with an exception: a transport forwards the reason as-is."""

    def __init__(self, session):
        self.session = session

    @property
    def _workflow(self):
        return self.session.workflow

    @property
    def _runner(self):
        return self.session.workflow.runner

    @property
    def permission_scope(self):
        return self.session.permission_scope

    def submit(self, action):
        """The one entry point: dispatch on the action class."""
        method = self._methods.get(type(action))
        if method is None:
            return Ack(
                accepted=False,
                reason=f"{type(action).__name__} names no action of this session. "
                f"The actions are {sorted(k.__name__ for k in self._methods)}.",
            )
        if self.session.has_ended:
            return self._refuse(
                action, f"{self.session.session_id} has ended and accepts no action."
            )
        # The permission check of the handler, from the marker its decorator
        # leaves (see authz.requires). The refusal is an ack like every other.
        scope_name = self.session.permission_scope
        reason = authz.check(method.requires, scope_name)
        ack = self._refuse(action, reason) if reason else method(self, action)
        authz.audit(
            action.name,
            scope_name,
            self.session.session_id,
            ack.accepted,
            ack.reason,
            action.audit_keys,
        )
        return ack

    def submit_guarded(self, action):
        """submit for a wire transport: an unexpected raise becomes a refused
        ack with the traceback in the session log, so no exception crosses the
        wire boundary. The TUI calls submit and keeps the real traceback."""
        try:
            return self.submit(action)
        except Exception:
            self._workflow.logger.error(
                f"{type(action).__name__} failed inside the session.", exc_info=True
            )
            return self._refuse(
                action,
                f"{type(action).__name__} failed inside the session - "
                f"the session log has the traceback.",
            )

    @classmethod
    def _refuse(cls, action, reason):
        return action.ack_class(accepted=False, reason=reason)

    @handles(RunTasks)
    @requires(Permission.RUN_TASK)
    def run_tasks(self, action):
        return self._submit_batch(
            action, self._runner.submit_run_single, self._runner.submit_run
        )

    @handles(CheckTasks)
    @requires(Permission.CHECK_TASK)
    def check_tasks(self, action):
        return self._submit_batch(
            action, self._runner.submit_check_single, self._runner.submit_check
        )

    def _batch_options_for(self, action):
        """The BatchOptions of one submit: the session baseline with the
        action's values on top, or (None, reason) on an unknown option."""
        overrides = action.options or {}
        known = {field.name for field in fields(BatchOptions)}
        unknown = sorted(set(overrides) - known)
        if unknown:
            return None, (
                f"{', '.join(repr(name) for name in unknown)} names no batch "
                f"option - the options are {sorted(known)}."
            )
        baseline = asdict(self._workflow.batch_options)
        return BatchOptions(**{**baseline, **overrides}), None

    def _submit_batch(self, action, submit_single, submit_bulk):
        keys = tuple(dict.fromkeys(action.keys))
        if len(keys) != len(action.keys):
            # A wire client can repeat a key; a batch runs each task once.
            dupes = sorted({key for key in keys if action.keys.count(key) > 1})
            self._workflow.logger.warning(
                f"{type(action).__name__} repeats {dupes}; each task enters the batch once."
            )
        options, reason = self._batch_options_for(action)
        if reason is not None:
            return self._refuse(action, reason)
        try:
            tasks = [self._workflow.task_index.resolve(key) for key in keys]
        except KeyError as exc:
            return self._refuse(action, exc.args[0])
        try:
            if len(tasks) == 1:
                batch = submit_single(tasks[0], options=options)
            else:
                batch = submit_bulk(tasks, options=options)
        except SessionEndingError as exc:
            return self._refuse(action, str(exc))
        if batch is None:
            # The admission filtered every task out (see _open_batch).
            return self._refuse(
                action,
                (
                    "no requested task is eligible for this action right now - "
                    "the current statuses exclude all of them; the task list "
                    "shows each status."
                ),
            )
        return BatchAck(accepted=True, batch_uuid=batch.uuid)

    @handles(StopBatch)
    @requires(Permission.CHECK_TASK, Permission.RUN_TASK)
    def stop_batch(self, action):
        batch = self._runner.get_batch(action.batch_uuid)
        if batch is None:
            return self._refuse(
                action, f"{action.batch_uuid} names no batch of this session."
            )
        # A stop needs the permission that could have submitted the batch.
        if batch.action is ExecutionAction.RUN and not authz.permitted(
            Permission.RUN_TASK, self.permission_scope
        ):
            return self._refuse(
                action, f"a run batch stops on {self.permission_scope}:run_task alone."
            )
        # The acceptance means "stop requested". The batch drains on its own
        # worker threads (see ExecutionBatch.request_stop).
        batch.request_stop()
        return Ack(accepted=True)

    @handles(EndSession)
    @requires(Permission.END_WORKFLOW, Permission.CHECK_TASK)
    def end_session(self, action):
        # A settled session, every task passing or skipped, ends on check_task
        # too: the work is done and nothing is lost.
        unsettled = self.session.unsettled_count
        if unsettled and not authz.permitted(
            Permission.END_WORKFLOW, self.permission_scope
        ):
            return self._refuse(
                action,
                f"{unsettled} task{'s' if unsettled != 1 else ''} not settled - "
                f"ending needs {self.permission_scope}:end_workflow.",
            )
        if action.force:
            self.session.force_end()
        else:
            self.session.end()
        return Ack(accepted=True)

    def _resolve_cache_entries(self, action):
        """(cache, entry_name) pairs for the wire pairs of the action, or a
        refusal reason naming the first unknown cache or entry."""
        caches_by_name = {cache.get_name(): cache for cache in self._workflow.caches()}
        resolved = []
        for cache_name, entry_name in action.entries:
            cache = caches_by_name.get(cache_name)
            if cache is None:
                return None, f"{cache_name!r} names no cache of this session."
            if entry_name not in declared_entries(type(cache)):
                return None, f"{cache} has no entry {entry_name!r}."
            resolved.append((cache, entry_name))
        return resolved, None

    def _load_cache_entry(self, cache, entry_name):
        # A loader failure is data, not an action failure: the entry reports
        # ERRORED and the session log carries the traceback (see
        # BaseCache._entry_value). The ack still accepts.
        try:
            getattr(cache, entry_name)
        except Exception:
            self._workflow.logger.error(
                f"Cache '{cache.get_name()}': the load of '{entry_name}' failed.",
                exc_info=True,
            )

    def _clear_cache_entry(self, cache, entry_name):
        cache.invalidate(entry_name)

    def _run_cache_entries(self, work, resolved):
        # A bare thread carries no LogContext. The scope routes the loader
        # and invalidation lines to the session log (see Session.log_scope).
        with self.session.log_scope():
            execute_in_threads(work, resolved)

    def _cache_entries_action(self, action, work):
        if not action.entries:
            return self._refuse(action, "the entries list is empty - nothing to do.")
        resolved, reason = self._resolve_cache_entries(action)
        if reason is not None:
            return self._refuse(action, reason)
        # The ack means "started", like RunTasks: execute_in_threads blocks
        # until every entry finishes, so a background thread runs it and
        # the caller does not wait. cache_updated events report progress.
        threading.Thread(
            target=contextvars.copy_context().run,
            args=(self._run_cache_entries, work, resolved),
            daemon=True,
        ).start()
        return Ack(accepted=True)

    @handles(LoadCacheEntries)
    @requires(Permission.RUN_TASK)
    def load_cache_entries(self, action):
        return self._cache_entries_action(action, self._load_cache_entry)

    @handles(ClearCacheEntries)
    @requires(Permission.RUN_TASK)
    def clear_cache_entries(self, action):
        return self._cache_entries_action(action, self._clear_cache_entry)


# The dispatch table of submit: action class -> method, from every method
# @handles marks. Adding an action means one dataclass and one marked method.
ActionHandler._methods = {
    method.handles: method for method in _tagged(ActionHandler, "handles")
}
