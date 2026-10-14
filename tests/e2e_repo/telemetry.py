"""The telemetry fixture of the e2e repo: a file-sink backend the one
telemetry test lists in TELEMETRY. The path of the sink comes from
WINSLOW_TEST_TELEMETRY_SINK, which that test sets."""

import json
import os

from winslow.exceptions import MisconfigurationError
from winslow.telemetry import TelemetryConfiguration, TelemetryHandler


class _FileSinkHandler(TelemetryHandler):
    def __init__(self, path):
        self.path = path

    def _write(self, record):
        with open(self.path, "a") as f:
            f.write(json.dumps(record) + "\n")

    def on_task_error(self, workflow, task, exc, batch_uuid, phase):
        self._write(
            {
                "kind": "task_error",
                "task": str(task),
                "exc": type(exc).__name__,
                "phase": phase.value,
            }
        )

    def on_unscoped_error(
        self,
        exc,
        workflow_name=None,
        session_id=None,
        workflow_instance=None,
        workflow_class=None,
    ):
        self._write(
            {
                "kind": "unscoped_error",
                "exc": type(exc).__name__,
                "workflow": workflow_name,
                "workflow_instance": workflow_instance,
                "workflow_class": workflow_class,
                "has_session": session_id is not None,
            }
        )


class FileSink(TelemetryConfiguration):
    def get_handler(self, orchestrator_config):
        self._path = os.environ.get("WINSLOW_TEST_TELEMETRY_SINK")
        if not self._path:
            raise MisconfigurationError("WINSLOW_TEST_TELEMETRY_SINK is unset")
        self._mode = orchestrator_config.mode.value
        return _FileSinkHandler(self._path)

    def shutdown(self):
        with open(self._path, "a") as f:
            f.write(json.dumps({"kind": "shutdown", "mode": self._mode}) + "\n")
