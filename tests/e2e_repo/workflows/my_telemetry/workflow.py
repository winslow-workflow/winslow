from target_base import TargetWorkflow


class MyTelemetry(TargetWorkflow):
    """The telemetry fixture: a task that calls sys.exit mid-run, plus a
    bystander - the SystemExit leg of the task error boundary, kept out of
    my-errors whose exact log output other tests pin down."""
