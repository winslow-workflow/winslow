"""The check_ttl gate: a passing snapshot younger than the effective TTL
replaces the probe. The fixture targets start empty, so a probe that runs
lands FAILED - the final status shows which path the runner took."""

import time

from winslow.session import Session
from winslow.state import StatusSnapshot
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


def start(workflow, state_store):
    Session(workflow, session_id=SID)
    workflow.check_pipeline_eligibility()
    workflow.init_state(state_store, origin="test")


def test_a_fresh_snapshot_skips_the_probe(e2e_repo, mode, state_store, monkeypatch):
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    alpha = by_name(workflow)["Alpha"]
    monkeypatch.setattr(type(alpha), "check_ttl", 3600)
    seed_entry(state_store, alpha)
    probes = count_probes(alpha, monkeypatch)
    start(workflow, state_store)

    check_batch(workflow, [alpha])

    assert workflow.store[alpha] is S.COMPLETED
    assert probes == []


def test_a_snapshot_beyond_the_ttl_probes(e2e_repo, mode, state_store, monkeypatch):
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    alpha = by_name(workflow)["Alpha"]
    monkeypatch.setattr(type(alpha), "check_ttl", 3600)
    seed_entry(state_store, alpha, age=7200.0)
    probes = count_probes(alpha, monkeypatch)
    start(workflow, state_store)

    check_batch(workflow, [alpha])

    assert workflow.store[alpha] is S.FAILED
    assert len(probes) == 1


def test_no_ttl_always_probes(e2e_repo, mode, state_store, monkeypatch):
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    alpha = by_name(workflow)["Alpha"]
    seed_entry(state_store, alpha)
    probes = count_probes(alpha, monkeypatch)
    start(workflow, state_store)

    check_batch(workflow, [alpha])

    assert workflow.store[alpha] is S.FAILED
    assert len(probes) == 1


def test_a_failed_snapshot_never_gates(e2e_repo, mode, state_store, monkeypatch):
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    alpha = by_name(workflow)["Alpha"]
    monkeypatch.setattr(type(alpha), "check_ttl", 3600)
    seed_entry(state_store, alpha, status="FAILED")
    probes = count_probes(alpha, monkeypatch)
    start(workflow, state_store)

    check_batch(workflow, [alpha])

    assert workflow.store[alpha] is S.FAILED
    assert len(probes) == 1


def test_dependency_resolution_honors_the_gate(
    e2e_repo, mode, state_store, monkeypatch
):
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    tasks = by_name(workflow)
    alpha, beta = tasks["Alpha"], tasks["Beta"]
    monkeypatch.setattr(type(alpha), "check_ttl", 3600)
    seed_entry(state_store, alpha)
    probes = count_probes(alpha, monkeypatch)
    start(workflow, state_store)

    run_batch(workflow, [beta])

    # The dependency probe of Beta resolved Alpha through the snapshot, so
    # Beta ran instead of blocking on an unverified dependency.
    assert workflow.store[alpha] is S.COMPLETED
    assert workflow.store[beta] is S.COMPLETED
    assert probes == []


def test_the_task_override_beats_the_workflow_default(
    e2e_repo, mode, state_store, monkeypatch
):
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    alpha = by_name(workflow)["Alpha"]
    monkeypatch.setattr(workflow, "check_ttl", 3600, raising=False)
    # The 60 second task TTL wins: the five minute old snapshot probes, where
    # the one hour workflow default would trust it.
    monkeypatch.setattr(type(alpha), "check_ttl", 60)
    seed_entry(state_store, alpha, age=300.0)
    probes = count_probes(alpha, monkeypatch)
    start(workflow, state_store)

    check_batch(workflow, [alpha])

    assert workflow.store[alpha] is S.FAILED
    assert len(probes) == 1


def test_the_workflow_default_gates_without_a_task_ttl(
    e2e_repo, mode, state_store, monkeypatch
):
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    alpha = by_name(workflow)["Alpha"]
    monkeypatch.setattr(workflow, "check_ttl", 3600, raising=False)
    seed_entry(state_store, alpha)
    probes = count_probes(alpha, monkeypatch)
    start(workflow, state_store)

    check_batch(workflow, [alpha])

    assert workflow.store[alpha] is S.COMPLETED
    assert probes == []


def test_a_gated_snapshot_does_not_refresh_the_stamp(
    e2e_repo, mode, state_store, monkeypatch
):
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    alpha = by_name(workflow)["Alpha"]
    monkeypatch.setattr(type(alpha), "check_ttl", 3600)
    seed_entry(state_store, alpha)
    stamped = state_store.load_status_snapshots(SID)[alpha.identity_key]
    start(workflow, state_store)

    check_batch(workflow, [alpha])

    # The replayed snapshot writes nothing: checked_at must not move without
    # a probe, because that would extend the trust window.
    assert state_store.load_status_snapshots(SID)[alpha.identity_key] == stamped
