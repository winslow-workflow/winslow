from target_base import TargetWorkflow


class MyBoomEligibility(TargetWorkflow):
    """The eligibility-defect fixture: is_eligible crashes, so the whole run
    aborts before any batch starts (see check_task_eligibility). Isolated in
    its own workflow because nothing else can run alongside it."""
