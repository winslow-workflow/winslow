"""The inbound action path of a session. A presentation layer translates
user input into one action dataclass and submits it to the ActionHandler of
the session. Action fields are values only: identity keys, scalars (the
payload rule, see winslow.events)."""

from dataclasses import asdict, dataclass, fields

from winslow.cache import declared_entries
from winslow.events import BatchOptionsChangedEvent
from winslow.exceptions import SessionEndingError
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
class RunTasks:
    keys: tuple


@dataclass(frozen=True)
class CheckTasks:
    keys: tuple


@dataclass(frozen=True)
class StopBatch:
    batch_uuid: str


@dataclass(frozen=True)
class EndSession:
    force: bool = False


@dataclass(frozen=True)
class SetBatchOptions:
    """A None field stays unchanged. The new values land in the stored
    manifest, so a restore rebuilds the toggles."""

    dry_run: bool | None = None
    force_run: bool | None = None
    force_success: bool | None = None
    disable_concurrency: bool | None = None


@dataclass(frozen=True)
class LoadCacheEntries:
    """Bulk-only, like RunTasks: a single selection sends a one-pair list.
    Each pair is (cache_name, entry_name)."""

    entries: tuple


@dataclass(frozen=True)
class ClearCacheEntries:
    """Bulk-only, like LoadCacheEntries. "Clear" is the action verb on the
    wire; the handler calls cache.invalidate internally."""

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

    def submit(self, action):
        """The one entry point: dispatch on the action class."""
        method = self._methods.get(type(action))
        if method is None:
            return self._refuse(
                action,
                f"{type(action).__name__} names no action of this session. "
                f"The actions are {sorted(k.__name__ for k in self._methods)}.",
            )
        if self.session.has_ended:
            return self._refuse(
                action, f"{self.session.session_id} has ended and accepts no action."
            )
        return method(self, action)

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
        ack_class = BatchAck if isinstance(action, (RunTasks, CheckTasks)) else Ack
        return ack_class(accepted=False, reason=reason)

    def run_tasks(self, action):
        return self._submit_batch(
            action, self._runner.submit_run_single, self._runner.submit_run
        )

    def check_tasks(self, action):
        return self._submit_batch(
            action, self._runner.submit_check_single, self._runner.submit_check
        )

    def _submit_batch(self, action, submit_single, submit_bulk):
        keys = tuple(dict.fromkeys(action.keys))
        if len(keys) != len(action.keys):
            # A wire client can repeat a key; a batch runs each task once.
            dupes = sorted({key for key in keys if action.keys.count(key) > 1})
            self._workflow.logger.warning(
                f"{type(action).__name__} repeats {dupes}; each task enters the batch once."
            )
        try:
            tasks = [self._workflow.task_index.resolve(key) for key in keys]
        except KeyError as exc:
            return self._refuse(action, exc.args[0])
        try:
            if len(tasks) == 1:
                batch = submit_single(tasks[0])
            else:
                batch = submit_bulk(tasks)
        except SessionEndingError as exc:
            return self._refuse(action, str(exc))
        if batch is None:
            # The admission filtered every task out (see _open_batch).
            return self._refuse(action, "The batch contains no eligible tasks.")
        return BatchAck(accepted=True, batch_uuid=batch.uuid)

    def stop_batch(self, action):
        batch = self._runner.get_batch(action.batch_uuid)
        if batch is None:
            return self._refuse(
                action, f"{action.batch_uuid} names no batch of this session."
            )
        # The acceptance means "stop requested". The batch drains on its own
        # worker threads (see ExecutionBatch.request_stop).
        batch.request_stop()
        return Ack(accepted=True)

    def end_session(self, action):
        if action.force:
            self.session.force_end()
        else:
            self.session.end()
        return Ack(accepted=True)

    def set_batch_options(self, action):
        options = self._workflow.batch_options
        for field in fields(SetBatchOptions):
            value = getattr(action, field.name)
            if value is not None:
                setattr(options, field.name, value)
        self._workflow.record_batch_options()
        self._workflow.bus.publish(BatchOptionsChangedEvent(options=asdict(options)))
        return Ack(accepted=True)

    def _caches(self):
        return (
            *self._workflow.workflow_cache.caches(),
            *self._workflow.global_cache.caches(),
        )

    def _resolve_cache_entries(self, action):
        """(cache, entry_name) pairs for the wire pairs of the action, or a
        refusal reason naming the first unknown cache or entry."""
        caches_by_name = {cache.get_name(): cache for cache in self._caches()}
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

    def _cache_entries_action(self, action, work):
        if not action.entries:
            return self._refuse(action, "the entries list is empty - nothing to do.")
        resolved, reason = self._resolve_cache_entries(action)
        if reason is not None:
            return self._refuse(action, reason)
        execute_in_threads(work, resolved)
        return Ack(accepted=True)

    def load_cache_entries(self, action):
        return self._cache_entries_action(action, self._load_cache_entry)

    def clear_cache_entries(self, action):
        return self._cache_entries_action(action, self._clear_cache_entry)

    # Adding an action means one dataclass and one method, registered here.
    _methods = {
        RunTasks: run_tasks,
        CheckTasks: check_tasks,
        StopBatch: stop_batch,
        EndSession: end_session,
        SetBatchOptions: set_batch_options,
        LoadCacheEntries: load_cache_entries,
        ClearCacheEntries: clear_cache_entries,
    }
