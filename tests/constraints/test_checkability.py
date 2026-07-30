from winslow.task.status import TaskStatus as S

from harness import UNPROCESSED, by_name, run_all

# The checkability gate refuses at the up-front completion check, before the
# success check or any run: CHECKING_COMPLETION is entered, then the block.
CHECK_BLOCKED_LADDER = UNPROCESSED + [S.CHECKING_COMPLETION, S.BLOCKED]


def test_failing_checkability_constraint_blocks(constraints_workflow):
    """A failing checkability constraint blocks the completion check itself,
    before the success check is ever consulted and before any run."""
    workflow = constraints_workflow
    run_all(workflow)
    tasks = by_name(workflow)

    workflow.store.assert_history_equals(
        tasks["ConstraintUncheckable"], CHECK_BLOCKED_LADDER
    )
    assert tasks["ConstraintUncheckable"] not in workflow.target
