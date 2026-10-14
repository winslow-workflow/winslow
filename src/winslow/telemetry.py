"""A generic hook for error telemetry.

A backend subclasses TelemetryHandler and activates through a
TelemetryConfiguration the TELEMETRY setting lists, or directly through
register_error_handler. Each error emits once, at the error boundaries. A
handler runs on the worker thread that hit the error: do not block. An
exception from a handler is logged and never reaches the batch (see
docs/telemetry.md).
"""

from winslow._base import derive_name
from winslow.logger import LOGGER


class TelemetryHandler:
    """The interface of a telemetry backend. Each callback default is a
    no-op, so a subclass overrides only the callbacks that it consumes."""

    def on_task_error(self, workflow, task, exc, batch_uuid, phase):
        """An errored task step (see BaseRunner.task_scope). Each errored
        step of a task emits one call. Do not retain the live objects."""

    def on_unscoped_error(
        self,
        exc,
        workflow_name=None,
        session_id=None,
        workflow_instance=None,
        workflow_class=None,
    ):
        """An error outside every task scope. workflow_instance carries
        str(workflow); each argument is None when it is not known yet."""


_handlers = []


def register_error_handler(handler):
    """Register a telemetry handler. More than one backend can register.
    Registration is not thread safe: register at startup, before a run."""
    _handlers.append(handler)


def unregister_error_handler(handler):
    """Remove a handler. An unknown handler is not an error."""
    if handler in _handlers:
        _handlers.remove(handler)


def get_registered_handlers():
    """A snapshot of the registered handlers."""
    return tuple(_handlers)


def get_registered_handler(handler_class):
    """The registered handler of this class, or None. Registration code
    uses it to stay idempotent: register one handler per backend."""
    for handler in _handlers:
        if isinstance(handler, handler_class):
            return handler
    return None


class TelemetryConfiguration:
    """The activation seam: a class the TELEMETRY setting lists by path,
    driven by the orchestrator (see docs/telemetry.md)."""

    def get_handler(self, orchestrator_config):
        """Return the TelemetryHandler to register. A configuration that
        cannot build raises MisconfigurationError, and the start stops."""
        raise NotImplementedError

    def shutdown(self):
        """Flush and release the backend, once, before the process exits."""

    @classmethod
    def get_name(cls):
        return derive_name(cls)


def activate_telemetry_configurations(configuration_classes, orchestrator_config):
    """Register the handler of each configuration, in order, and return the
    [(configuration, handler)] pairs. Every listed configuration answers a
    handler or stops the start (see TelemetryConfiguration.get_handler)."""
    active = []
    try:
        for kls in configuration_classes:
            configuration = kls()
            handler = configuration.get_handler(orchestrator_config)
            register_error_handler(handler)
            active.append((configuration, handler))
            LOGGER.debug(f"Telemetry configuration {configuration} activated.")
    except BaseException:
        # A later configuration that cannot build leaves no earlier handler
        # behind, so the next start of an embedding process is clean.
        shutdown_telemetry_configurations(active)
        raise
    return active


def shutdown_telemetry_configurations(active):
    """Unregister the handlers and flush the backends, last first. A shutdown
    that raises is logged: a failing backend must not mask the run result."""
    for configuration, handler in reversed(active):
        unregister_error_handler(handler)
        try:
            configuration.shutdown()
        except Exception:
            LOGGER.error(
                f"Telemetry configuration {configuration!r} raised in shutdown",
                exc_info=True,
            )


def emit_task_error(workflow, task, exc, batch_uuid, phase):
    """Report an errored task step to each registered handler."""
    for handler in _handlers:
        try:
            handler.on_task_error(workflow, task, exc, batch_uuid, phase)
        except Exception:
            LOGGER.error(
                f"Telemetry handler {handler!r} raised in on_task_error",
                exc_info=True,
            )


def emit_unscoped_error(
    exc,
    workflow_name=None,
    session_id=None,
    workflow_instance=None,
    workflow_class=None,
):
    """Report an error outside every task scope to each registered handler."""
    for handler in _handlers:
        try:
            handler.on_unscoped_error(
                exc,
                workflow_name=workflow_name,
                session_id=session_id,
                workflow_instance=workflow_instance,
                workflow_class=workflow_class,
            )
        except Exception:
            LOGGER.error(
                f"Telemetry handler {handler!r} raised in on_unscoped_error",
                exc_info=True,
            )
