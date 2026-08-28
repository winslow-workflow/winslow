"""The check_ttl trust rule lives in the state writers: restore seeding
writes a trusted success as its status and an untrusted one as STALE, and
the sweeper expires a live status (see test_stale_sweeper). A check itself
always probes. The fixture targets start empty, so a probe that runs lands
FAILED - the final status shows which path the runner took."""

import time

from winslow.session import Session
from winslow.model import StatusSnapshot
from winslow.task.status import TaskStatus as S

from harness import build_workflow, by_name, check_batch, run_batch

# The snapshot reads key by session id, so the tests seed under a fixed id
# and start the session with that same id, like a restore does.
SID = "ttl-20260819T000000-00000001"


def seed_entry(state_store, task, status="COMPLETED", age=0.0):
    state_store.save_status_snapshot(
        SID,
        StatusSnapshot(key=task.identity_key, status=status, checked_at=time.time() - age),
    )


def count_probes(task, monkeypatch):
    kls, original, calls = type(task), type(task).check, []

    def check(self):
        calls.append(self)
        return original(self)

    monkeypatch.setattr(kls, "check", check)
    return calls


def start_seeded(workflow, state_store):
    Session(workflow, session_id=SID)
    workflow.check_pipeline_eligibility()
    workflow.init_state(state_store, origin="test")
    workflow.seed_from_state()


def test_a_fresh_snapshot_seeds_completed_and_skips_the_run(
    e2e_repo, mode, state_store, monkeypatch
):
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    alpha = by_name(workflow)["Alpha"]
    monkeypatch.setattr(type(alpha), "check_ttl", 3600)
    seed_entry(state_store, alpha)
    probes = count_probes(alpha, monkeypatch)
    start_seeded(workflow, state_store)

    assert workflow.store[alpha] is S.COMPLETED

    run_batch(workflow, [alpha])

    # A passing store status skips the pre-run check: no probe, no run.
    assert workflow.store[alpha] is S.COMPLETED
    assert probes == []


def test_a_snapshot_beyond_the_ttl_seeds_stale_and_probes(
    e2e_repo, mode, state_store, monkeypatch
):
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    alpha = by_name(workflow)["Alpha"]
    monkeypatch.setattr(type(alpha), "check_ttl", 3600)
    seed_entry(state_store, alpha, age=7200.0)
    probes = count_probes(alpha, monkeypatch)
    start_seeded(workflow, state_store)

    assert workflow.store[alpha] is S.STALE

    check_batch(workflow, [alpha])

    assert workflow.store[alpha] is S.FAILED
    assert len(probes) == 1


def test_no_ttl_seeds_an_old_success_as_stale(e2e_repo, mode, state_store, monkeypatch):
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    alpha = by_name(workflow)["Alpha"]
    seed_entry(state_store, alpha)
    probes = count_probes(alpha, monkeypatch)
    start_seeded(workflow, state_store)

    # Without a TTL only a verification of this session counts, and the
    # entry predates the session start.
    assert workflow.store[alpha] is S.STALE

    check_batch(workflow, [alpha])

    assert workflow.store[alpha] is S.FAILED
    assert len(probes) == 1


def test_a_failed_snapshot_seeds_failed(e2e_repo, mode, state_store, monkeypatch):
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    alpha = by_name(workflow)["Alpha"]
    monkeypatch.setattr(type(alpha), "check_ttl", 3600)
    seed_entry(state_store, alpha, status="FAILED")
    probes = count_probes(alpha, monkeypatch)
    start_seeded(workflow, state_store)

    assert workflow.store[alpha] is S.FAILED

    check_batch(workflow, [alpha])

    assert workflow.store[alpha] is S.FAILED
    assert len(probes) == 1


def test_dependency_resolution_trusts_the_seeded_status(
    e2e_repo, mode, state_store, monkeypatch
):
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    tasks = by_name(workflow)
    alpha, beta = tasks["Alpha"], tasks["Beta"]
    monkeypatch.setattr(type(alpha), "check_ttl", 3600)
    seed_entry(state_store, alpha)
    probes = count_probes(alpha, monkeypatch)
    start_seeded(workflow, state_store)

    run_batch(workflow, [beta])

    # Alpha seeded COMPLETED, so the dependency pass of Beta had nothing to
    # resolve: Beta ran instead of blocking on an unverified dependency.
    assert workflow.store[alpha] is S.COMPLETED
    assert workflow.store[beta] is S.COMPLETED
    assert probes == []


def test_the_task_override_beats_the_workflow_default(
    e2e_repo, mode, state_store, monkeypatch
):
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    alpha = by_name(workflow)["Alpha"]
    monkeypatch.setattr(workflow, "check_ttl", 3600, raising=False)
    # The 60 second task TTL wins: the five minute old entry seeds STALE,
    # where the one hour workflow default would seed COMPLETED.
    monkeypatch.setattr(type(alpha), "check_ttl", 60)
    seed_entry(state_store, alpha, age=300.0)
    start_seeded(workflow, state_store)

    assert workflow.store[alpha] is S.STALE


def test_the_workflow_default_applies_without_a_task_ttl(
    e2e_repo, mode, state_store, monkeypatch
):
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    alpha = by_name(workflow)["Alpha"]
    monkeypatch.setattr(workflow, "check_ttl", 3600, raising=False)
    seed_entry(state_store, alpha)
    start_seeded(workflow, state_store)

    assert workflow.store[alpha] is S.COMPLETED


def test_an_explicit_check_always_probes(e2e_repo, mode, state_store, monkeypatch):
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    alpha = by_name(workflow)["Alpha"]
    monkeypatch.setattr(type(alpha), "check_ttl", 3600)
    probes = count_probes(alpha, monkeypatch)
    start_seeded(workflow, state_store)

    run_batch(workflow, [alpha])
    assert workflow.store[alpha] is S.COMPLETED
    probes.clear()

    check_batch(workflow, [alpha])

    # A check is an explicit re-verification: the TTL never suppresses it.
    assert workflow.store[alpha] is S.COMPLETED
    assert len(probes) == 1
