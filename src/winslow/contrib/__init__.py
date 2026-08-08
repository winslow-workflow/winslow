"""Optional telemetry backends for the hook in winslow.telemetry.

The core never imports the backend modules, so their extras stay
optional: this package root imports one only after its gate passes.
"""

import importlib
import sys

from winslow.settings import config

from winslow.exceptions import MisconfigurationError


def default_telemetry_configurations():
    """The built-in configuration classes whose environment gate passes.
    A gate that passes without its extra raises MisconfigurationError."""
    backends = (
        (_sentry_configured, "sentry", "SentryConfiguration"),
        (_opentelemetry_configured, "otel", "OpenTelemetryConfiguration"),
    )
    return [
        _configuration_class(module_name, class_name)
        for gate, module_name, class_name in backends
        if gate()
    ]


def _sentry_configured():
    if config("SENTRY_DSN", default=None):
        return True
    # The active client of a host application also activates; read through
    # sys.modules, so the gate never imports the SDK.
    sentry_sdk = sys.modules.get("sentry_sdk")
    return sentry_sdk is not None and sentry_sdk.get_client().is_active()


def _opentelemetry_configured():
    return bool(
        config("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", default=None)
        or config("OTEL_EXPORTER_OTLP_ENDPOINT", default=None)
    )


def _configuration_class(module_name, class_name):
    try:
        module = importlib.import_module(f"winslow.contrib.{module_name}")
    except ImportError as e:
        raise MisconfigurationError(
            f"The environment activates the {module_name} telemetry backend, but "
            f"its dependencies are not installed - install with: "
            f"pip install 'winslow[{module_name}]'"
        ) from e
    return getattr(module, class_name)
