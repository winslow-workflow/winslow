import json
import logging

import pytest

from winslow.logger import (
    RUNS_LOGGER_NAME,
    StructuredFormatter,
    setup_run_logging,
    shutdown_run_logging,
)

from harness import build_workflow, run_all


class MemorySink(logging.Handler):
    """Stands in for the file sink's disk write only - CI may run on a
    read-only filesystem. StructuredFormatter is the production component
    ("same bytes to disk and wire"), so each collected line is exactly what
    SessionFileSink would have written."""

    def __init__(self):
        super().__init__()
        self.setFormatter(StructuredFormatter())
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


@pytest.fixture
def log_sink():
    """The production wiring with an in-memory sink. Teardown restores the
    process-global seam (queue handler, propagate boundary) because other
    tests share this process and production never re-wires in-process."""
    sink = MemorySink()
    setup_run_logging(sinks=[sink])
    yield sink
    shutdown_run_logging()
    runs_logger = logging.getLogger(RUNS_LOGGER_NAME)
    for handler in list(runs_logger.handlers):
        runs_logger.removeHandler(handler)
    runs_logger.propagate = True


def test_task_errors_reach_the_session_log(e2e_repo, mode, log_sink):
    """End to end through the real seam: task logger > queue > listener >
    structured JSON lines. Only WARNING+ lands (the harness never initializes
    console logging, so the runs logger inherits the root threshold), which
    pins the output to exactly the defect records."""
    workflow = build_workflow(e2e_repo, "my-errors", mode)

    run_all(workflow)
    session_id = workflow.session.session_id

    # Drains the queue synchronously - the sink is complete after this. The
    # fixture's later call is a no-op (idempotent).
    shutdown_run_logging()

    records = [json.loads(line) for line in log_sink.lines]

    # Exactly the two defects, stamped with this session and their tasks -
    # the session_id stamp is also the file sink's routing key, so correct
    # stamps mean correct per-session files.
    assert [(r["session_id"], r["task_instance"], r["level"]) for r in records] == [
        (session_id, "boom", "ERROR"),
        (session_id, "partial-boom", "ERROR"),
    ]
    # The bounded name fields, next to the instances: a sink indexes and
    # groups by them (see LogContext).
    for record in records:
        assert record["workflow_name"] == "my-errors"
        assert record["task_name"] == record["task_instance"]
    # The queue handler formats before the thread hop, so the full traceback
    # (raise site included) survives into the sink - errors are diagnosable
    # from the log alone.
    for record, message in zip(records, ("boom", "partial boom")):
        assert record["message"].startswith(message)
        assert "Traceback (most recent call last)" in record["message"]
        assert "tasks/errors.py" in record["message"]


def _record(**kwargs):
    return logging.LogRecord(
        name="winslow", level=logging.ERROR, pathname=__file__, lineno=1,
        msg="boom happened", args=(), **kwargs,
    )


def test_the_structured_formatter_carries_the_traceback():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _record(exc_info=sys.exc_info())
    payload = json.loads(StructuredFormatter().format(record))
    assert "ValueError: boom" in payload["traceback"]
    assert payload["message"] == "boom happened"

    plain = json.loads(StructuredFormatter().format(_record(exc_info=None)))
    assert plain["traceback"] is None


def test_the_console_emits_json_under_the_env_switch(monkeypatch):
    import winslow.logger as logger_module

    monkeypatch.setattr(logger_module, "LOG_JSON", True)
    handler = logger_module._console_handler()
    assert isinstance(handler.formatter, StructuredFormatter)

    monkeypatch.setattr(logger_module, "LOG_JSON", False)
    handler = logger_module._console_handler()
    assert not isinstance(handler.formatter, StructuredFormatter)


def test_the_stdout_json_sink_shares_the_structured_shape():
    import sys

    from winslow.logger import stdout_json_sink

    sink = stdout_json_sink()
    assert isinstance(sink.formatter, StructuredFormatter)
    assert sink.stream is sys.stdout
