"""The inbound action path of a session. A presentation layer translates
user input into one action dataclass and submits it to the ActionHandler of
the session. Action fields are values only: identity keys, scalars (the
payload rule, see winslow.events)."""

from dataclasses import dataclass, fields

from winslow.exceptions import SessionEndingError


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
        batch = self._runner.execution_batches_map.get(action.batch_uuid)
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
        return Ack(accepted=True)

    # Adding an action means one dataclass and one method, registered here.
    _methods = {
        RunTasks: run_tasks,
        CheckTasks: check_tasks,
        StopBatch: stop_batch,
        EndSession: end_session,
        SetBatchOptions: set_batch_options,
    }
