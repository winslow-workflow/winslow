from winslow import ConfigOption

from target_base import TargetWorkflow


class MyIdentified(TargetWorkflow):
    """The identifier fixture: two runs of this workflow differ by --client,
    and telemetry must carry that identity."""

    client = ConfigOption(identifier=True, help_text="The client of this run.")
