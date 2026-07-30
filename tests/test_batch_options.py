from winslow.task.status import TaskStatus as S

from harness import (
    BLOCKED_LADDER,
    CHECK_PASSED_LADDER,
    COMPLETED_LADDER,
    DEP_BLOCKED_LADDER,
    FAILED_LADDER,
    RUN_AND_CHECK,
    SKIPPED_LADDER,
    UNPROCESSED,
    build_workflow,
    by_name,
    run_all,
)

# force_run skips the up-front completion check - no CHECKING_COMPLETION >
# FAILED prefix - but everything after it still applies: dependencies,
# runnability, and the post-run validation.
FORCE_RUN_PRE = UNPROCESSED + [S.CHECKING_DEPENDENCIES, S.CHECKING_RUNNABILITY]
FORCE_RUN_COMPLETED = FORCE_RUN_PRE + RUN_AND_CHECK + [S.COMPLETED]
FORCE_RUN_FAILED = FORCE_RUN_PRE + RUN_AND_CHECK + [S.FAILED]
FORCE_RUN_BLOCKED = FORCE_RUN_PRE + [S.BLOCKED]
# The dry-run variant: RUNNING appears, but run() never executes, so a
# lenient check that passes afterwards lands COMPLETED_PREVIOUSLY.
FORCE_DRY_COMPLETED = FORCE_RUN_PRE + RUN_AND_CHECK + [S.COMPLETED_PREVIOUSLY]

# With force_run there is no up-front check to fail first - a task blocks on
# its failed dependency straight after the dependency phase.
FORCE_DRY_DEP_BLOCKED = UNPROCESSED + [S.CHECKING_DEPENDENCIES, S.BLOCKED]


def test_non_strict_check_trusts_existence(e2e_repo, mode):
    """The control case for force_run: a non-True marker satisfies a lenient
    check, so a normal run leaves it untouched - while the same marker on a
    strict task fails the up-front check and gets overwritten by a real run."""
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    tasks = by_name(workflow)
    workflow.target[tasks["Lenient"]] = "foo"
    workflow.target[tasks["Alpha"]] = "foo"

    run_all(workflow)

    # Existence satisfied the lenient check up front - no run, marker intact.
    workflow.store.assert_history_equals(tasks["Lenient"], CHECK_PASSED_LADDER)
    assert workflow.target[tasks["Lenient"]] == "foo"
    # The strict check rejects the foreign marker, so Alpha really ran - and
    # the overwritten value is the evidence.
    workflow.store.assert_history_equals(tasks["Alpha"], COMPLETED_LADDER)
    assert workflow.target[tasks["Alpha"]] is True


def test_force_run_redoes_completed_work(e2e_repo, mode):
    """force_run means "redo the work even though it looks done": the up-front
    completion check is skipped, so seeded tasks run again. Unlike
    force_success, the outcome is still validated - the post-run check stays."""
    workflow = build_workflow(e2e_repo, "my-workflow", mode, "--force-run")
    tasks = by_name(workflow)
    # "foo" markers would pass Lenient's check untouched on a normal run (see
    # test_non_strict_check_trusts_existence) - finding True afterwards is
    # direct evidence the run happened, independent of the ladder.
    for name in ("Alpha", "Beta", "Gamma", "Lenient", "Blocked"):
        workflow.target[tasks[name]] = "foo"

    run_all(workflow)

    # RUNNING despite looking done, and the marker overwritten by run().
    for name in ("Alpha", "Beta", "Gamma", "Lenient"):
        workflow.store.assert_history_equals(tasks[name], FORCE_RUN_COMPLETED)
        assert workflow.target[tasks[name]] is True
    # The post-run check is still consulted - force_run is not force_success:
    # a forced run that produced nothing must fail.
    workflow.store.assert_history_equals(tasks["FailingCheck"], FORCE_RUN_FAILED)
    # Forcing the run also forces the gates: Blocked's marker would have
    # passed its up-front check, but the flag pushes it onto the run path
    # where its refusing can_run gate now matters - and because it never ran,
    # its marker survives.
    workflow.store.assert_history_equals(tasks["Blocked"], FORCE_RUN_BLOCKED)
    assert workflow.target[tasks["Blocked"]] == "foo"
    # Eligibility still outranks every batch option.
    workflow.store.assert_history_equals(tasks["Ineligible"], SKIPPED_LADDER)


def test_dry_run_does_no_work(e2e_repo, mode):
    """Dry run swaps run() for dry_run(): the full run path is walked, but no
    real work happens - so the honest post-run FAILED cascades down the
    dependency chain exactly as a real failure would."""
    workflow = build_workflow(e2e_repo, "my-workflow", mode, "--dry-run")
    tasks = by_name(workflow)

    run_all(workflow)

    # The dry-run contract: nothing was written anywhere.
    assert workflow.target == {}

    # Alpha "ran" (dry) and failed its post-run check; the trailing extra
    # check is Beta's dependency resolution giving it a second chance.
    workflow.store.assert_history_equals(
        tasks["Alpha"], FAILED_LADDER + [S.CHECKING_COMPLETION, S.FAILED]
    )
    # Beta blocks on Alpha's failure before its own runnability gate, then is
    # re-checked (and honestly fails) when Gamma resolves its dependencies.
    workflow.store.assert_history_equals(
        tasks["Beta"], DEP_BLOCKED_LADDER + [S.CHECKING_COMPLETION, S.FAILED]
    )
    # Gamma blocks on Beta and, as the chain's leaf, is nobody's dependency -
    # no re-check, BLOCKED is terminal.
    workflow.store.assert_history_equals(tasks["Gamma"], DEP_BLOCKED_LADDER)

    # Chain-independent tasks behave as in a real run - the dry swap only
    # changes what run() does, not which gates fire.
    workflow.store.assert_history_equals(tasks["FailingCheck"], FAILED_LADDER)
    workflow.store.assert_history_equals(tasks["Blocked"], BLOCKED_LADDER)
    workflow.store.assert_history_equals(tasks["Ineligible"], SKIPPED_LADDER)


def test_force_run_with_dry_run(e2e_repo, mode):
    """Both flags together: force_run pushes done-looking work onto the run
    path, dry_run makes that run a no-op. Every marker must survive - and the
    outcome is then decided purely by how strict each task's check is."""
    workflow = build_workflow(e2e_repo, "my-workflow", mode, "--force-run", "--dry-run")
    tasks = by_name(workflow)
    seeded = ("Alpha", "Beta", "Gamma", "Lenient", "Blocked")
    for name in seeded:
        workflow.target[tasks[name]] = "foo"

    run_all(workflow)

    # The dry-run contract holds even under force: nothing was overwritten.
    assert all(workflow.target[tasks[name]] == "foo" for name in seeded)

    # Strict Alpha dry-"ran", produced nothing valid, and fails - plus the
    # re-check from Beta's dependency resolution.
    workflow.store.assert_history_equals(
        tasks["Alpha"], FORCE_RUN_FAILED + [S.CHECKING_COMPLETION, S.FAILED]
    )
    # No up-front check under force_run, so Beta blocks straight after the
    # dependency phase; Gamma's resolution then re-checks it (still "foo",
    # still strict, still FAILED).
    workflow.store.assert_history_equals(
        tasks["Beta"], FORCE_DRY_DEP_BLOCKED + [S.CHECKING_COMPLETION, S.FAILED]
    )
    workflow.store.assert_history_equals(tasks["Gamma"], FORCE_DRY_DEP_BLOCKED)

    # The trap this combination documents: a lenient check passes on the
    # surviving marker - existence checks can't tell a dry run from a real
    # one. COMPLETED_PREVIOUSLY at least records that this workflow never
    # ran the work.
    workflow.store.assert_history_equals(tasks["Lenient"], FORCE_DRY_COMPLETED)

    workflow.store.assert_history_equals(tasks["FailingCheck"], FORCE_RUN_FAILED)
    # Gates fire as usual; Blocked never reaches the (dry) run.
    workflow.store.assert_history_equals(tasks["Blocked"], FORCE_RUN_BLOCKED)
    workflow.store.assert_history_equals(tasks["Ineligible"], SKIPPED_LADDER)
