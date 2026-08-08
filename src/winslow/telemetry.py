"""A generic hook for error telemetry.

A backend subclasses TelemetryHandler and activates through a
TelemetryConfiguration or directly through register_error_handler. Each
error emits once, at the error boundaries. A handler runs on the worker
thread that hit the error: do not block. An exception from a handler is
logged and never reaches the batch (see docs/telemetry.md).
"""

from winslow._base import derive_name
from winslow.logger import LOGGER
from winslow.registry import Registry


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
    """The configuration-first activation seam: declared in the telemetry.py
    files of a repo, driven by the orchestrator (see docs/telemetry.md)."""

    def get_handler(self, orchestrator_config):
        """Return the TelemetryHandler to register, or None to stay
        inactive. An exception fails the start of the run loudly."""
        return None

    def shutdown(self):
        """Flush and release the backend, once, before the process exits."""

    @classmethod
    def get_name(cls):
        return derive_name(cls)


class TelemetryRegistry(Registry):
    """Collects the TelemetryConfiguration classes of a repo from its
    telemetry.py files."""

    item_class = TelemetryConfiguration
    file_filter = {"telemetry.py"}


def activate_telemetry_configurations(configuration_classes, orchestrator_config):
    """Register the handler of each active configuration and return the
    [(configuration, handler)] pairs. Only leaf classes activate: a repo
    subclass replaces the built-in it inherits from."""
    # A lazy import: the contrib modules import this module, so a
    # module-level import would make a cycle.
    from winslow.contrib import default_telemetry_configurations

    defaults = list(default_telemetry_configurations())
    classes = defaults + [kls for kls in configuration_classes if kls not in defaults]
    leaves = [
        kls
        for kls in classes
        if not any(other is not kls and issubclass(other, kls) for other in classes)
    ]

    active = []
    for kls in sorted(leaves, key=lambda kls: kls.get_name()):
        configuration = kls()
        handler = configuration.get_handler(orchestrator_config)
        if handler is None:
            continue
        register_error_handler(handler)
        active.append((configuration, handler))
        LOGGER.debug(f"Telemetry configuration {configuration} activated.")
    return active


def shutdown_telemetry_configurations(active):
    """Unregister the handlers and flush the backends. A shutdown that
    raises is logged: a failing backend must not mask the run result."""
    for configuration, handler in active:
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
