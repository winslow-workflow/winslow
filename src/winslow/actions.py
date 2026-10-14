"""The inbound action path of a session. A client submits one Action
declaration of the port (see winslow.client.base.SessionClient) and the
ActionHandler of the session answers with an ack. Action fields are values
only: identity keys, scalars (the payload rule, see winslow.events)."""

import contextvars
import threading
from dataclasses import asdict, dataclass, fields

from winslow.cache import declared_entries
from winslow.access import authz
from winslow.access.permissions import Permission
from winslow.runner.execution import ExecutionAction
from winslow.exceptions import SessionEndingError
from winslow.model import SessionInfo
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


@dataclass(frozen=True)
class SessionAck(Ack):
    """The answer to a session start or restore (see AppActionHandler). An
    accepted one carries the SessionInfo of the live session. A refusal from
    the init code of the workflow carries its traceback in detail, for the
    error modal of a client."""

    session: SessionInfo | None = None
    detail: str | None = None


class BaseActionHandler:
    """The guarded submit both handlers share (see ActionHandler,
    AppActionHandler). submit answers an ack and writes the audit line. A
    raise inside it is a bug of the handler: submit_guarded turns it into a
    refused ack with its own audit line, so no exception crosses a door."""

    # The logger of the handler and how a refusal names its log.
    logger = None
    log_name = "the log"

    def _audit_fields(self, action):
        """The (scope name, session id) of the audit line of a guarded raise."""
        raise NotImplementedError

    def submit(self, action):
        raise NotImplementedError

    def submit_guarded(self, action):
        """submit for a wire transport: an unexpected raise becomes a refused
        ack with the traceback in the log. The TUI calls submit and keeps
        the real traceback."""
        try:
            return self.submit(action)
        except Exception:
            spec = action.spec
            self.logger.error(f"{spec.name} failed inside the handler.", exc_info=True)
            reason = (
                f"{spec.name} failed inside the handler - {self.log_name} has the "
                f"traceback."
            )
            scope_name, session_id = self._audit_fields(action)
            authz.audit(
                spec.name, scope_name, session_id, False, reason, spec.count(action)
            )
            return spec.refused(reason)


class ActionHandler(BaseActionHandler):
    """One per session: the inbound half of the session boundary (the bus is
    the outbound half, see SessionBus). The handler accepts one action,
    resolves its values to live objects, gates it, delegates it to the runner
    or the session, and answers with an ack. It refuses with an ack, never
    with an exception: a transport forwards the reason as-is."""

    log_name = "the session log"

    def __init__(self, session):
        self.session = session

    @property
    def logger(self):
        return self.session.workflow.logger

    def _audit_fields(self, action):
        return self.session.permission_scope, self.session.session_id

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
        """The one entry point: the value of one Action declaration
        dispatches to the method of its name (see Action.__call__). An ended
        session and a missing permission refuse through the ack, and every
        outcome leaves one audit line."""
        spec = action.spec
        scope_name = self.session.permission_scope
        reason = self._ended_reason() or authz.check(spec.requires, scope_name)
        ack = (
            self._refuse(action, reason) if reason else getattr(self, spec.name)(action)
        )
        authz.audit(
            spec.name,
            scope_name,
            self.session.session_id,
            ack.accepted,
            ack.reason,
            spec.count(action),
        )
        return ack

    def _ended_reason(self):
        if not self.session.has_ended:
            return None
        return f"{self.session.session_id} has ended and accepts no action."

    @classmethod
    def _refuse(cls, action, reason):
        return action.spec.refused(reason)

    def run_tasks(self, action):
        return self._submit_batch(
            action, self._runner.submit_run_single, self._runner.submit_run
        )

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
                f"{action.spec.name} repeats {dupes}; each task enters the batch once."
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

    def load_cache_entries(self, action):
        return self._cache_entries_action(action, self._load_cache_entry)

    def clear_cache_entries(self, action):
        return self._cache_entries_action(action, self._clear_cache_entry)
