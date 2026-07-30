from target_base import TargetWorkflow


class MyCheckErrors(TargetWorkflow):
    """The check-defect fixture: checks that raise instead of answering -
    before the run ever starts (BoomCheck) and over the artifact the run
    itself landed (ChokesOnArtifact)."""
