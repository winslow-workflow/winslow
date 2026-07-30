from target_base import TargetWorkflow


class MyConstraints(TargetWorkflow):
    """The constraints fixture: one task per ConstraintType, each declaring a
    composable constraint (not the equivalent hook override) so a real run
    proves the constraint drives the same status ladder the override would."""
