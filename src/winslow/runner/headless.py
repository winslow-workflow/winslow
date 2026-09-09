import threading
from itertools import groupby

from winslow.task import TaskStatus
from winslow.events import BatchCompletedEvent, BatchCreatedEvent
from winslow.exceptions import TaskBlock
from winslow.cache import batch_cache
from winslow.model import BatchInfo

from .base import BaseRunner
from .execution import ExecutionAction, new_batch


class HeadlessRunner(BaseRunner):
    def _open_batch(self, action, tasks, options=None):
        """Create the batch, on the thread of the submitter. The body of the
        admission gate is atomic against Session.end(). A session that ends
        refuses the batch with the typed error before any task work. A batch that
        is registered holds the store open until it drains. Returns (None, [])
        when no task is eligible, and creates no batch."""
        with self.workflow.session.batch_admission():
            tasks = [t for t in tasks if not self._refuse_ineligible(t)]
            if not tasks:
                return None, []
            batch = new_batch(action, tasks)
            batch.execution_context = self._new_execution_context(batch.uuid, options)
            # The whole store, and not the task list of the batch. A dependency
            # re-check reaches tasks outside the batch with the uuid of this
            # batch.
            batch.errored = {
                k
                for k, s in self.store.items()
                if s in (TaskStatus.ERROR, TaskStatus.COMPLETED_WITH_ERROR)
            }
            self._execution_batches_map[batch.uuid] = batch
        batch.start()
        self._batch_admitted(batch, tasks)
        # On the submitter thread, before any task work: a crash during the
        # batch must leave the open record behind (see SessionPersistenceAdapter).
        self.workflow.bus.publish(BatchCreatedEvent(BatchInfo.from_batch(batch, tasks)))
        return batch, tasks

    def _execute_batch(self, batch, tasks, body):
        """The full life of the worker. InteractiveRunner extends _batch_started
        and _batch_finished with the execution record stores and the lifecycle
        events."""
        with batch_cache(batch.uuid):
            self._batch_started(batch, tasks)
            try:
                body(batch, tasks)
            except Exception as e:
                # Log here, where the error occurs. A submit without a wait() has
                # no place to raise the error again, and a batch failure that
                # crosses a thread boundary must not be silent.
                self.logger.error(f"batch {batch.uuid[:8]} raised", exc_info=True)
                batch.record_error(e)
            finally:
                # Call complete() first. The _batch_finished hook reads the
                # final status, and the completed event carries it.
                batch.complete()
                self._batch_finished(batch, tasks)
                self.workflow.bus.publish(
                    BatchCompletedEvent(BatchInfo.from_batch(batch, tasks))
                )
                # The drain rule: an ending session finalizes when its last
                # batch completes. After the publish, so every subscriber sees
                # the completion before the finalization closes the bus.
                if (session := self.workflow.session) is not None:
                    session.finalize_if_drained()

    def _submit(self, action, tasks, body, options=None):
        batch, tasks = self._open_batch(action, tasks, options)
        if batch is None:
            return None
        worker = threading.Thread(
            target=self._execute_batch,
            args=(batch, tasks, body),
            name=f"winslow-batch-{batch.uuid[:8]}",
        )
        batch.attach_worker(worker)
        worker.start()
        return batch

    def _batch_admitted(self, batch, tasks):
        """Hook on the submitter thread, before the created event publishes.
        The interactive runner registers the record store here, so a created
        subscriber can read it."""

    def _batch_started(self, batch, tasks):
        pass

    def _batch_finished(self, batch, tasks):
        pass

    def submit_check(self, tasks, options=None):
        return self._submit(ExecutionAction.CHECK, tasks, self._check_body, options)

    def submit_run(self, tasks, options=None):
        return self._submit(ExecutionAction.RUN, tasks, self._run_body, options)

    def check(self, tasks):
        if batch := self.submit_check(tasks):
            batch.wait()

    def run(self, tasks):
        if batch := self.submit_run(tasks):
            batch.wait()

    def _check_body(self, batch, tasks):
        self._check_tasks(tasks, batch.uuid)

    def _run_body(self, batch, tasks):
        for _, group in self._priority_groups(tasks):
            self._process_group(list(group), batch.uuid)

    def _priority_groups(self, tasks):
        return groupby(
            sorted(tasks, key=lambda t: t.execution_order),
            key=lambda t: t.execution_order,
        )

    def _resolve_group_dependencies(self, group, batch_uuid):
        """Probe the dependencies of the group one time, before the fan-out. The
        sibling threads then share one settled result for each dependency and do
        not race. Returns the dependencies that were resolved."""
        ctx = self._execution_context_for(batch_uuid)
        if ctx.force_success and not ctx.force_run:
            return (
                frozenset()
            )  # force_success completes a task and does not touch its dependencies
        deps = {dep for task in group for dep in task.dependent_tasks}
        if deps:
            # Not filtered. A dependency that failed before can pass now. Each
            # group probes again and shares the new result.
            self._resolve_dependencies(deps, batch_uuid)
        return frozenset(deps)

    def _process_group(self, group, batch_uuid):
        checked_deps = self._resolve_group_dependencies(group, batch_uuid)
        parallel = [t for t in group if t.can_process_parallel]
        serial = [t for t in group if not t.can_process_parallel]
        if parallel:
            self.logger.debug(f"Processing in parallel: {parallel}")
            self._batch_call(
                self._process_task_guarded, parallel, batch_uuid, checked_deps
            )
        if serial:
            self.logger.debug(f"Processing serially: {serial}")
            for task in serial:
                self._process_task_guarded(task, batch_uuid, checked_deps)

    def _process_task_guarded(self, task, batch_uuid, checked_deps=frozenset()):
        self.logger.debug(f"Processing: {task}")
        try:
            # A noop task has no run action. Check its completion instead.
            if task.is_noop:
                self._check_task_success(task, batch_uuid)
            else:
                self.process_task(task, batch_uuid, checked_deps)
        except TaskBlock:
            self.set_status(task, TaskStatus.BLOCKED, batch_uuid)
