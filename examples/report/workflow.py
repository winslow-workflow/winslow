from pathlib import Path

from winslow import Workflow, Task, ConfigOption


class Report(Workflow):
    region = ConfigOption(
        type=str,
        required=True,
        choices=["eu", "us"],
        help_text="The region to report on.",
    )
    limit = ConfigOption(type=int, default=10, help_text="The maximum row count.")


class WriteReport(Task):
    @property
    def output_path(self):
        return Path(f"report-{self.workflow_config.region}.txt")

    def run(self):
        self.output_path.write_text(f"rows: {self.workflow_config.limit}\n")

    def check(self):
        return self.output_path.exists()
