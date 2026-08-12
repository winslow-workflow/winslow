from contextlib import contextmanager
from functools import partial

from winslow.task import TaskStatus
from winslow._base import _Base
from winslow.logger import LOGGER

from winslow.exceptions import (
    TaskBlock,
    TaskFailure,
    TaskActionRequired,
    TaskSignal,
    IllegalTaskOutcomeError,
)

from winslow.task.status import (
    ACTIVE_STATUSES,
    PASSING_STATUSES,
    CHECKABLE_STATUSES,
    UNSATISFIED_STATUSES,
)
from winslow.task.context import (
    TaskExecutionContext,
    scoped_execution_context,
    LogContext,
    scoped_log_context,
)

from winslow.util import execute_in_threads
from winslow.cache import reset_phase_cache
from winslow.task.eligibility import check_task_eligibility
from winslow.telemetry import emit_task_error

from .execution import ExecutionPhase


class BaseRunner(_Base):
    # The stop latency of the waits in _resolve_dependencies. A stop
    # request writes no status to the store, so it cannot end a wait by
    # itself: it sets an event on the batch (see
    # ExecutionBatch.request_stop), and only _stop_requested reads it. The
    # timeout ends each wait after 0.5 seconds, and the loop reads the
    # event between the slices. The constant is thus the longest delay
    # between a stop request and its detection.
    #
    # Example: a task waits on a dependency that runs for 60 seconds, and
    # the user presses stop after 10. The waiter wakes at the end of its
    # current 0.5 second slice, reads _stop_requested, and leaves the wait.
    # Without the timeout it would sleep the full 60 seconds.
    TASK_DEPENDENCY_STOP_POLL_SECONDS = 0.5

    def __init__(
        self,
        orchestrator_config,
        workflow,
        store,
        workflow_config,
        batch_options,
        logger=LOGGER,
    ):
        super().__init__(orchestrator_config)
        self.workflow = workflow
        self.store = store
        self.logger = logger
        self.workflow_config = workflow_config
        # The workflow and the UI share this object and change it live.
        self.batch_options = batch_options
        # On the base class, so both runners share it. History and the UI look up
        # a batch by its uuid.
        self.execution_batches_map = {}

    def _new_execution_context(self, batch_uuid):
        """Snapshot the batch options. A later change in the UI does not affect
        the batch, and history shows the options that the batch used."""
        o = self.batch_options
        return TaskExecutionContext(
            batch_uuid=batch_uuid,
            dry_run=o.dry_run,
            force_run=o.force_run,
            force_success=o.force_success,
            disable_concurrency=o.disable_concurrency,
        )

    def _execution_context_for(self, batch_uuid):
        return self.execution_batches_map[batch_uuid].execution_context

    @property
    def active_batches(self):
        # Iterate over a list. Iteration over the dict fails if another thread
        # updates the map during the read.
        return [
            b
            for b in list(self.execution_batches_map.values())
            if b.completed_at is None
        ]

    def release_batch_errors(self):
        """Drop the error tracebacks of every retained batch, so no raise-path
        frame keeps a task alive past the session end."""
        for batch in list(self.execution_batches_map.values()):
            batch.release_traceback()

    def _log_context(self, task, batch_uuid):
        return LogContext(
            session_id=self.workflow.session_id,
            workflow_name=self.workflow.instance_name,
            workflow_instance=str(self.workflow),
            task_name=task.instance_name,
            task_instance=str(task),
            batch_uuid=batch_uuid,
        )

    @contextmanager
    def task_log_scope(self, task, batch_uuid):
        """In this scope the log records of the task carry the full context
        stamps. A subclass can extend the scope, for example to copy the records
        into the execution record of the batch."""
        with scoped_log_context(self._log_context(task, batch_uuid)):
            yield

    @contextmanager
    def task_scope(self, task, batch_uuid, phase):
        """Each step of a task runs in this wrapper.

        The scopes, from the outside in:

            task_log_scope                 # stamps the log records, so the
                                           # tracebacks below reach the
                                           # execution record of the batch
              scoped_execution_context     # resolves the flags and the
                                           # transient properties
                reset_phase_cache          # only when the phase is a
                                           # checkability gate
                  yield                    # the step body

        Cache lifecycle: each checkability gate opens a logical pass, and
        the phases after the gate inherit its cache. A value that the gate
        materialized is reused downstream, never calculated again, because
        a second calculation can disagree with the decision. The post-run
        gate opens a new pass: completion is verified against the state
        after the run (see TransientProperty for a full trace).

        The handlers below convert the escapes of the step body into ERROR:
        sys.exit, an illegal task signal, and any other exception. They also
        report the error to the telemetry hook, even when reraise_errors is
        set. The paths upstream must not report the exception again (see
        telemetry.py).
        """
        with self.task_log_scope(task, batch_uuid):
            try:
                with scoped_execution_context(
                    self._execution_context_for(batch_uuid)
                ) as ctx:
                    if phase.resets_cache:
                        reset_phase_cache(task, batch_uuid)
                    yield ctx
            except SystemExit as e:
                # A sys.exit() in task code escapes as a BaseException. It aborts
                # the batch and leaves the task in RUNNING. This handler prevents
                # that. KeyboardInterrupt is not caught here, because Ctrl-C must
                # still abort the batch.
                task.logger.error(
                    f"task called sys.exit({e.code}); treating as error", exc_info=True
                )
                self.set_status(task, TaskStatus.ERROR, batch_uuid)
                self.logger.error(f"{task} called sys.exit({e.code}) - marked errored")
                emit_task_error(self.workflow, task, e, batch_uuid, phase)

                if self.orchestrator_config.reraise_errors:
                    raise
            except TaskSignal as e:
                # A signal that arrives here escaped every handler in the body of
                # the step. The hook raised a signal that is not legal for this
                # step, for example skip outside eligibility. The outcome is
                # ERROR, as for any defect, but the message tells the author what
                # to correct.
                msg = f"{type(e).__name__} is not a legal signal during {phase}"
                task.logger.error(msg, exc_info=True)
                self.set_status(task, TaskStatus.ERROR, batch_uuid)
                self.logger.error(f"{task}: {msg} - marked errored")
                emit_task_error(self.workflow, task, e, batch_uuid, phase)

                if self.orchestrator_config.reraise_errors:
                    # Do not raise the signal again. A handler upstream would
                    # accept it as legal flow control.
                    raise IllegalTaskOutcomeError(msg) from e
            except Exception as e:
                task.logger.error(e, exc_info=True)
                self.set_status(task, TaskStatus.ERROR, batch_uuid)
                self.logger.error(
                    f"{task} errored: {e} (full traceback visible in task logs)"
                )
                emit_task_error(self.workflow, task, e, batch_uuid, phase)

                if self.orchestrator_config.reraise_errors:
                    raise

    def set_status(self, task, status, batch_uuid):
        # The current runners abort on ERROR, but a custom runner may
        # introduce retry logic in a batch. An errored task can then run
        # again in the same batch, and the RUNNING write must reset the flag.
        batch = self.execution_batches_map.get(batch_uuid)
        if batch is not None:
            if status is TaskStatus.RUNNING:
                batch.errored.discard(task.uuid)
            elif status is TaskStatus.ERROR:
                batch.errored.add(task.uuid)
        self.store[task] = status

    def _mirror_batch_status(self, task, status, batch_uuid):
        """Copy the status from the task store to the batch store when the task
        was not processed, for example when it was already completed."""

    def _stop_requested(self, batch_uuid):
        return False

    def _check_task_runnability(self, task, batch_uuid):
        self.logger.debug(f"Checking runnability: {task}")
        self.set_status(task, TaskStatus.CHECKING_RUNNABILITY, batch_uuid)

        with self.task_scope(task, batch_uuid, ExecutionPhase.RUNNABILITY):
            try:
                result = task._evaluate_can_run()
                status = TaskStatus.READY_TO_RUN if result else TaskStatus.BLOCKED
                self.set_status(task, status, batch_uuid)
            except TaskBlock as e:
                task.logger.error(e)
                self.set_status(task, TaskStatus.BLOCKED, batch_uuid)

    def _check_task_eligibility(self, task, _):  # The signature matches _batch_call
        # This does not run in task_scope. An eligibility check that fails must
        # abort the workflow init (see check_task_eligibility) and must not
        # become a silent ERROR.
        self.set_status(task, TaskStatus.CHECKING_ELIGIBILITY, None)
        result = check_task_eligibility(task, logger=self.logger)
        self.set_status(
            task,
            TaskStatus.READY_TO_PROCESS if result else TaskStatus.SKIPPED,
            None,
        )

    def _refuse_ineligible(self, task):
        if self.store[task] is TaskStatus.SKIPPED:
            self.logger.debug(f"{task} is SKIPPED (ineligible) - not touching it.")
            return True
        return False

    def _force_success(self, task, ctx, batch_uuid):
        """If force_success is set, mark a checkable task FORCE_SUCCESS and do
        not run its checks. Return True if this method handled the task."""
        if not ctx.force_success:
            return False
        if self.store[task] in CHECKABLE_STATUSES:
            self.set_status(task, TaskStatus.FORCE_SUCCESS, batch_uuid)
        return True

    def _check_task_success(self, task, batch_uuid, phase=ExecutionPhase.CHECK):
        if self._refuse_ineligible(task):
            return

        if self._force_success(
            task, self._execution_context_for(batch_uuid), batch_uuid
        ):
            return

        self.logger.debug(f"Checking success: {task}")

        self.set_status(task, TaskStatus.CHECKING_COMPLETION, batch_uuid)

        with self.task_scope(task, batch_uuid, phase.checkability):
            # The gate can only block. fail, skip and action_required defeat its
            # purpose, which is to prevent a false FAILED. This handler accepts
            # only block. Each other signal goes to the illegal-signal handler in
            # task_scope.
            try:
                if not task._evaluate_can_check():
                    self.set_status(task, TaskStatus.BLOCKED, batch_uuid)
                    return
            except TaskBlock as e:
                task.logger.error(e)
                self.set_status(task, TaskStatus.BLOCKED, batch_uuid)
                return

        with self.task_scope(task, batch_uuid, phase):
            try:
                result = task._evaluate_check()

                if result:
                    # A check that passes does not remove a defect. A task that
                    # errored after its last run attempt stays flagged.
                    batch = self.execution_batches_map.get(batch_uuid)
                    flagged = batch is not None and task.uuid in batch.errored
                    if flagged:
                        stat = TaskStatus.COMPLETED_WITH_ERROR
                    elif task.is_noop or task._has_been_run:
                        stat = TaskStatus.COMPLETED
                    else:
                        stat = TaskStatus.COMPLETED_PREVIOUSLY
                else:
                    stat = TaskStatus.FAILED
                self.set_status(task, stat, batch_uuid)
            except TaskBlock as e:
                task.logger.error(e)
                self.set_status(task, TaskStatus.BLOCKED, batch_uuid)
            except TaskActionRequired as e:
                task.logger.info(str(e))
                self.set_status(task, TaskStatus.ACTION_REQUIRED, batch_uuid)
            except TaskFailure as e:
                task.logger.error(e)
                self.set_status(task, TaskStatus.FAILED, batch_uuid)

    def _run_task(self, task, batch_uuid):
        with self.task_scope(task, batch_uuid, ExecutionPhase.RUN) as ctx:
            self.set_status(task, TaskStatus.RUNNING, batch_uuid)

            if ctx.dry_run:
                self.logger.info(f"Dry-running: {task}")
                task.dry_run()
            else:
                self.logger.info(f"Running {task}")
                # Before run(), so a run that raises still counts as a run
                # attempt of this workflow (see TaskStatus.COMPLETED_PREVIOUSLY).
                task._has_been_run = True
                task.run()
            self.set_status(task, TaskStatus.RUN_FINISHED, batch_uuid)

    def _batch_call(self, method, tasks, batch_uuid, *args):
        if self._concurrency_disabled(batch_uuid):
            for task in tasks:
                method(task, batch_uuid, *args)
        else:
            execute_in_threads(method, [(t, batch_uuid, *args) for t in tasks])

    def _concurrency_disabled(self, batch_uuid):
        # Eligibility runs before the batch (batch_uuid is None) and has no
        # snapshot, so read the live options. In a batch, read the snapshot of
        # the batch to stay consistent.
        if batch_uuid is None:
            return self.batch_options.disable_concurrency
        return self._execution_context_for(batch_uuid).disable_concurrency

    def _check_tasks(self, tasks, batch_uuid):
        parallel = [t for t in tasks if t.can_check_parallel]
        serial = [t for t in tasks if not t.can_check_parallel]
        if parallel:
            self.logger.debug(f"Checking in parallel: {parallel}")
            self._batch_call(self._check_task_success, parallel, batch_uuid)
        if serial:
            self.logger.debug(f"Checking serially: {serial}")
            for task in serial:
                self._check_task_success(task, batch_uuid)

    def check_eligibility(self, tasks):
        self._batch_call(
            self._check_task_eligibility,
            tasks,
            None,
        )

    def _any_settled(self, deps):
        return any(self.store[dep] not in ACTIVE_STATUSES for dep in deps)

    def check_dependencies(self, task, batch_uuid=None, checked_deps=frozenset()):
        self.set_status(task, TaskStatus.CHECKING_DEPENDENCIES, batch_uuid)
        self._resolve_dependencies(task.dependent_tasks, batch_uuid, checked_deps)

    def _resolve_dependencies(self, deps, batch_uuid, checked_deps=frozenset()):
        # Example: task D depends on A, B and C. The group pre-pass probed
        # B, so the caller passes checked_deps={B}. The statuses at entry:
        #
        #     A: COMPLETED            # satisfied
        #     B: FAILED               # probed by the pre-pass
        #     C: CHECKING_COMPLETION  # a sibling thread probes C right now
        #
        # The walk:
        #
        #     1. candidates = [B, C]. A is satisfied and drops out.
        #     2. C is active: wait_for = [C]. B is in checked_deps, so no
        #        new probe: good_to_check stays empty.
        #     3. The loop waits for C in 0.5 second slices (see
        #        TASK_DEPENDENCY_STOP_POLL_SECONDS).
        #     4. C settles as COMPLETED and leaves the wait list, and the
        #        loop ends. If C settles as FAILED instead, no pre-pass
        #        covered it, so the loop probes C once before it ends.
        #     5. The caller reads the statuses: B stays unsatisfied, so D
        #        becomes BLOCKED (see process_task).
        #
        # The tricky parts:
        #
        # - checked_deps holds the dependencies that the caller resolved in
        #   the group pre-pass. The statuses do not show them, so the caller
        #   passes them, and the sibling threads skip exactly these probes,
        #   which would otherwise race.
        # - A dependency in an active status belongs to another thread at
        #   that moment. The loop waits instead of probing it, and sorts it
        #   again when it settles: still unsatisfied means probe it here,
        #   any other status means it is resolved.
        # - The wait runs in short slices, so a stop request is noticed.

        # TODO: raise a timeout here

        candidates = [dep for dep in deps if self.store[dep] in UNSATISFIED_STATUSES]

        wait_for, good_to_check = [], []

        for dep in candidates:
            stat = self.store[dep]

            if stat in ACTIVE_STATUSES:
                wait_for.append(dep)
            elif dep not in checked_deps:
                good_to_check.append(dep)

        while wait_for or good_to_check:
            if good_to_check:
                self.logger.debug(f"Processing ready tasks: {good_to_check}")
                self._check_tasks(good_to_check, batch_uuid)
                good_to_check = []

            if wait_for:
                self.logger.debug(f"Waiting for active tasks: {wait_for}")
                if self._stop_requested(batch_uuid):
                    break
                self.store.wait_for_state(
                    partial(self._any_settled, wait_for),
                    timeout=self.TASK_DEPENDENCY_STOP_POLL_SECONDS,
                )

                still_waiting = []
                for dep in wait_for:
                    stat = self.store[dep]
                    if stat in ACTIVE_STATUSES:
                        still_waiting.append(dep)
                    elif stat in UNSATISFIED_STATUSES and dep not in checked_deps:
                        good_to_check.append(dep)

                wait_for = still_waiting

    def process_task(self, task, batch_uuid, checked_deps=frozenset()):
        """
        The steps of a task, in order:

        - If force_success is set and force_run is not, mark the task
          FORCE_SUCCESS and stop. No check, dependency, runnability or run step
          occurs. This applies where a check would run, which is a checkable
          status. An ineligible (skipped) task is thus not touched. If force_run
          is also set, force_run has priority: the task runs and the final check
          applies force_success.
        - The initial success check. It is omitted if force_run is set.
        - The runnability check.
        - The dependency check.
        - The run or the dry run.
        - The success check again.
        """
        if self._refuse_ineligible(task):
            return

        ctx = self._execution_context_for(batch_uuid)

        # force_run has priority: run the task. The final check after the run
        # applies force_success.
        if not ctx.force_run and self._force_success(task, ctx, batch_uuid):
            return

        dependencies = task.dependent_tasks

        if ctx.force_run:
            self.logger.info(f"FORCE RUN: Skipping initial completion check for {task}")
        elif (status := self.store[task]) in PASSING_STATUSES:
            self.logger.debug(f"{task} is already marked as {status}")
            self._mirror_batch_status(task, status, batch_uuid)
            return
        else:
            self._check_task_success(task, batch_uuid, ExecutionPhase.PRE_RUN_CHECK)
            # Run only after a verified "not complete", which is FAILED. This is
            # an allowlist and not a blocklist. BLOCKED, ERROR and
            # ACTION_REQUIRED do not confirm that a run is safe. A run without
            # that confirmation can break idempotency.
            if self.store[task] is not TaskStatus.FAILED:
                return

        self.check_dependencies(task, batch_uuid, checked_deps)

        failed_deps = [
            dep for dep in dependencies if self.store[dep] in UNSATISFIED_STATUSES
        ]

        if failed_deps:
            self.set_status(task, TaskStatus.BLOCKED, batch_uuid)
            raise TaskBlock(
                f"Cannot run {task} - {len(failed_deps)} dependencies are failing."
            )

        # This runs after the dependency check, because the runnability check can
        # depend on the effects of the completed dependencies.
        self._check_task_runnability(task, batch_uuid)

        # A runnability check that errored must stay ERROR. The TaskBlock
        # handlers upstream must not change it to BLOCKED.
        if self.store[task] is TaskStatus.ERROR:
            return

        if self.store[task] is not TaskStatus.READY_TO_RUN:
            raise TaskBlock(
                f"{task} is not marked as {TaskStatus.READY_TO_RUN} - aborting."
            )

        self._run_task(task, batch_uuid)

        if self.store[task] is TaskStatus.ERROR:
            return

        self._check_task_success(task, batch_uuid, ExecutionPhase.POST_RUN_CHECK)
