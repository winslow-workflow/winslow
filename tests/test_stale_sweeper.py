"""The sweeper: a passing status whose check TTL lapses flips to STALE while
the session runs, as an ordinary store write."""

from winslow.session import Session
from winslow.task.status import TaskStatus as S

from harness import build_workflow, by_name, run_batch


def test_a_lapsed_ttl_flips_the_status_live(e2e_repo, state_store, mode, monkeypatch):
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    alpha = by_name(workflow)["Alpha"]
    monkeypatch.setattr(type(alpha), "check_ttl", 0.2)
    Session(workflow)
    workflow.check_pipeline_eligibility()
    workflow.init_state(state_store, origin="test")

    run_batch(workflow, [alpha])
    assert workflow.store[alpha] in (S.COMPLETED, S.STALE)

    assert workflow.store.wait_for_state(
        lambda: workflow.store[alpha] is S.STALE, timeout=5
    )
    # The flip never persists: the snapshot keeps the real outcome.
    entry = workflow.load_snapshot(alpha.identity_key)
    assert entry.status == S.COMPLETED.name


def test_no_ttl_never_flips(e2e_repo, state_store, mode):
    workflow = build_workflow(e2e_repo, "my-workflow", mode)
    alpha = by_name(workflow)["Alpha"]
    Session(workflow)
    workflow.check_pipeline_eligibility()
    workflow.init_state(state_store, origin="test")

    run_batch(workflow, [alpha])

    # No TTL: in-session trust never expires, so the sweeper has no deadline.
    assert workflow.stale_sweeper._sweep() is None
    assert workflow.store[alpha] is S.COMPLETED
