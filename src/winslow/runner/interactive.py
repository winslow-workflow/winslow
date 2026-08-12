import functools
import threading
from contextlib import contextmanager, nullcontext

from winslow.cache import peek_phase_cache
from winslow.decorators import snapshot_transients
from winslow.exceptions import TaskBlock
from winslow.logger import InteractiveLogHandler, INLINE_FORMATTER, get_task_dispatcher
from winslow.task import TaskStatus

from .execution import ExecutionAction, ExecutionPhase
from .headless import HeadlessRunner
from .store import ExecutionRecordStore


def claims_task(method):
    """Guard between batches. This batch claims the task for the duration of the
    call. If another batch owns the task, refuse the call and do not execute."""

    @functools.wraps(method)
    def wrapper(self, task, batch_uuid, *args, **kwargs):
        with self._claim_task(task, batch_uuid) as claimed:
            if claimed:
                return method(self, task, batch_uuid, *args, **kwargs)

    return wrapper


class InteractiveRunner(HeadlessRunner):
    # This limits the stop latency while a thread waits on a claimed task. A
    # release (see _release_claim) wakes the waiter immediately, but a stop
    # request sends no notification.
    CLAIM_STOP_POLL_SECONDS = 0.5

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.execution_record_store_map: dict[str, ExecutionRecordStore] = {}
        # The thread id is part of the claim, so only same-thread reentrancy
        # (process -> check on one task) passes. Two threads of the same batch
        # that reach one task must wait, as in any other clash.
        self._active_tasks: dict = {}  # task -> (owning batch_uuid, thread id)
        self._claims = (
            threading.Condition()
        )  # protects _active_tasks, notified on a release

    def set_status(self, task, status, batch_uuid):
        super().set_status(task, status, batch_uuid)
        # A dependency that is probed for the batch is not part of the batch.
        # Only the tasks of the batch get an execution record.
        store = self.execution_record_store_map.get(batch_uuid) if batch_uuid else None
        if store is not None and task.uuid in store:
            store[task.uuid] = status

    def _track(self, batch_uuid, task, phase):
        store = self.execution_record_store_map[batch_uuid]
        if task.uuid not in store:
            return nullcontext()
        return store.get_record(task.uuid).track_phase(phase)

    def _claim_free_or_stopped(self, task, batch_uuid):
        return self._active_tasks.get(task) is None or self._stop_requested(batch_uuid)

    def _wait_for_release(self, task, batch_uuid):
        """Wait under the claims condition until the owner releases the task or
        this batch stops. A release wakes the waiter immediately. The timeout
        slice is necessary only for a stop, which sends no notification."""
        with self.task_log_scope(task, batch_uuid):
            task.logger.info(
                f"{task} is claimed by another batch - waiting for release"
            )
        self._mirror_batch_status(task, TaskStatus.WAITING_FOR_RELEASE, batch_uuid)
        free = functools.partial(self._claim_free_or_stopped, task, batch_uuid)
        while not self._claims.wait_for(free, timeout=self.CLAIM_STOP_POLL_SECONDS):
            pass

    def _holds_claim(self, claimant, task):
        with self._claims:
            return self._active_tasks.get(task) == claimant

    def _acquire_claim(self, claimant, task, batch_uuid):
        """Take the claim of the task and wait until another batch releases its
        ownership. Returns False only if the wait ended because this batch
        stopped. A stop has priority over a release at the same time, so a
        stopped batch never claims a task."""
        with self._claims:
            if self._active_tasks.get(task) is not None:
                self._wait_for_release(task, batch_uuid)
                if self._stop_requested(batch_uuid):
                    return False
            self._active_tasks[task] = claimant
            return True

    def _release_claim(self, task):
        with self._claims:
            self._active_tasks.pop(task, None)
            self._claims.notify_all()

    def _abort_stopped_wait(self, task, batch_uuid):
        with self.task_log_scope(task, batch_uuid):
            task.logger.warning(
                f"{task} was still claimed when the batch stopped - aborted."
            )
        # The status is batch-local. The batch that owns the task controls the
        # main store.
        self._mirror_batch_status(task, TaskStatus.ABORTED, batch_uuid)

    @contextmanager
    def _claim_task(self, task, batch_uuid):
        claimant = (batch_uuid, threading.get_ident())
        if self._holds_claim(claimant, task):
            # Same-thread reentrancy (process -> check) uses the outer claim.
            yield True
            return
        if not self._acquire_claim(claimant, task, batch_uuid):
            self._abort_stopped_wait(task, batch_uuid)
            yield False
            return
        try:
            yield True
        finally:
            self._release_claim(task)

    def _mirror_batch_status(self, task, status, batch_uuid):
        store = self.execution_record_store_map[batch_uuid]
        if task.uuid in store:
            store[task.uuid] = status

    @contextmanager
    def task_log_scope(self, task, batch_uuid):
        store = self.execution_record_store_map[batch_uuid]
        if task.uuid not in store:
            with super().task_log_scope(task, batch_uuid):
                yield
            return
        record = store.get_record(task.uuid)
        handlers = (
            InteractiveLogHandler(record.append_log),
            InteractiveLogHandler(
                record.notify_display_log, formatter=INLINE_FORMATTER
            ),
        )
        dispatcher = get_task_dispatcher()
        with (
            dispatcher.listen(task.uuid, *handlers),
            super().task_log_scope(task, batch_uuid),
        ):
            yield

    @contextmanager
    def task_scope(self, task, batch_uuid, phase):
        try:
            with (
                self._track(batch_uuid, task, phase),
                super().task_scope(task, batch_uuid, phase) as ctx,
            ):
                yield ctx
        finally:
            # The cache is reset at the next checkability gate only, so it is
            # still complete here. Snapshot the values that this phase
            # materialized.
            store = self.execution_record_store_map[batch_uuid]
            if task.uuid in store:
                if snapshot := snapshot_transients(
                    task, peek_phase_cache(task, batch_uuid)
                ):
                    store.get_record(task.uuid).transient_snapshots[phase] = snapshot

    def _stop_requested(self, batch_uuid):
        batch = self.execution_batches_map.get(batch_uuid)
        return batch is not None and batch.stop_requested

    def _abort_if_stopped(self, task, batch_uuid) -> bool:
        if self._stop_requested(batch_uuid):
            self.set_status(task, TaskStatus.ABORTED, batch_uuid)
            return True
        return False

    @claims_task
    def process_task(self, task, batch_uuid, checked_deps=frozenset()):
        if self._abort_if_stopped(task, batch_uuid):
            return
        super().process_task(task, batch_uuid, checked_deps)

    @claims_task
    def _check_task_success(self, task, batch_uuid, phase=ExecutionPhase.CHECK):
        if self._abort_if_stopped(task, batch_uuid):
            return
        super()._check_task_success(task, batch_uuid, phase)

    def _batch_started(self, batch, tasks):
        exec_store = ExecutionRecordStore(batch.uuid, [])
        # The same observers as the main store. They receive the execution events
        # of this batch (on_execution_status and on_log_appended) with the live
        # status.
        for listener in self.store.listeners:
            exec_store.add_listener(listener)

        for task in tasks:
            exec_store.register(task)

        self.execution_record_store_map[batch.uuid] = exec_store
        self.store.emit_batch_created(batch)

    def _batch_finished(self, batch, tasks):
        # The sweep replaces each stub with a full capture, so history shows
        # the attribute values after this execution (see TaskInfo.from_task).
        store = self.execution_record_store_map[batch.uuid]
        for task in tasks:
            store.capture(task)
        self.store.emit_batch_completed(batch)

    def eligible_tasks(self, tasks):
        return [t for t in tasks if not self._refuse_ineligible(t)]

    def submit_check_single(self, task):
        return self._submit(ExecutionAction.CHECK, [task], self._single_check_body)

    def submit_run_single(self, task):
        return self._submit(ExecutionAction.RUN, [task], self._single_run_body)

    def check_single(self, task):
        if batch := self.submit_check_single(task):
            batch.wait()

    def run_single(self, task):
        if batch := self.submit_run_single(task):
            batch.wait()

    def bulk_check(self, tasks):
        self.check(tasks)

    def bulk_run(self, tasks):
        self.run(tasks)

    def _single_check_body(self, batch, tasks):
        self._check_task_success(tasks[0], batch.uuid)

    def _single_run_body(self, batch, tasks):
        try:
            self.process_task(tasks[0], batch.uuid)
        except TaskBlock:
            self.set_status(tasks[0], TaskStatus.BLOCKED, batch.uuid)

    def _abort_unstarted(self, tasks, batch):
        store = self.execution_record_store_map[batch.uuid]
        for task in tasks:
            if store.get(task.uuid) is TaskStatus.READY_TO_PROCESS:
                self.set_status(task, TaskStatus.ABORTED, batch.uuid)

    # The stop sweep is interactive only. A headless batch has no way to request
    # a stop, so the base bodies run without a sweep.

    def _check_body(self, batch, tasks):
        super()._check_body(batch, tasks)
        if batch.stop_requested:
            self._abort_unstarted(tasks, batch)

    def _run_body(self, batch, tasks):
        for _, group in self._priority_groups(tasks):
            if batch.stop_requested:
                break
            self._process_group(list(group), batch.uuid)
        if batch.stop_requested:
            self._abort_unstarted(tasks, batch)
