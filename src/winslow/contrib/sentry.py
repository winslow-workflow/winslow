"""Error reporting to Sentry through the telemetry hook.

With SENTRY_DSN set, the orchestrator activates SentryConfiguration for
each run; subclass it in a telemetry.py file to customize. A host
application that owns its process calls setup_sentry instead. Requires
the [sentry] extra (see docs/telemetry.md).
"""

try:
    import sentry_sdk
except ImportError:
    raise ImportError(
        "sentry-sdk is not installed. Install the extra: pip install winslow[sentry]"
    ) from None

import logging

from sentry_sdk.integrations.logging import LoggingIntegration

from winslow import settings
from winslow.settings import config
from winslow.telemetry import (
    TelemetryConfiguration,
    TelemetryHandler,
    register_error_handler,
    get_registered_handler,
)


class SentryTelemetryHandler(TelemetryHandler):
    """Reports each telemetry error as one Sentry event, in a fresh
    isolation scope: batches error concurrently across threads."""

    def on_task_error(self, workflow, task, exc, batch_uuid, phase):
        with sentry_sdk.isolation_scope() as scope:
            _set_tags(
                scope,
                workflow=workflow.instance_name,
                workflow_class=type(workflow).__name__,
                # str(workflow): two configured runs differ by it.
                workflow_instance=str(workflow),
                # The declared name, symmetric with the workflow tag.
                task=task.instance_name,
                task_class=type(task).__name__,
                # str(task): the declared name plus the parameter values.
                task_instance=str(task),
                # Sorted and comma-joined, so the tag stays deterministic.
                groups=task.groups_readable or None,
                session_id=workflow.session_id,
                batch_uuid=batch_uuid,
                phase=phase.value,
            )
            if task._is_parameterized:
                scope.set_extra("parameters", task._parameters_dict_safe)
            if workflow.identifiers_dict_safe:
                scope.set_extra("identifiers", workflow.identifiers_dict_safe)
            scope.fingerprint = self._fingerprint(str(workflow), str(task), exc)
            sentry_sdk.capture_exception(exc)

    def on_unscoped_error(
        self,
        exc,
        workflow_name=None,
        session_id=None,
        workflow_instance=None,
        workflow_class=None,
    ):
        with sentry_sdk.isolation_scope() as scope:
            _set_tags(
                scope,
                workflow=workflow_name,
                workflow_instance=workflow_instance,
                workflow_class=workflow_class,
                session_id=session_id,
            )
            scope.fingerprint = self._fingerprint(
                workflow_instance or workflow_name, None, exc
            )
            sentry_sdk.capture_exception(exc)

    @classmethod
    def _fingerprint(cls, workflow_instance, task_instance, exc):
        """Issues collapse only for the same configured run and the same
        task row; the absent task part of an unscoped error is skipped."""
        parts = [workflow_instance or "unknown", task_instance]
        return [part for part in parts if part] + [type(exc).__name__]


def _set_tags(scope, **tags):
    """Sentry serializes a None tag as the string "None": skip it instead,
    so an absent field stays absent in the issue view."""
    for key, value in tags.items():
        if value is not None:
            scope.set_tag(key, value)


def _init_sdk_if_needed(dsn, init_kwargs):
    """Initialize the SDK unless a host application already holds an active
    client: PII off, and logs as breadcrumbs only, so the task boundary
    stays the single event source."""
    if sentry_sdk.get_client().is_active():
        return
    defaults = {
        "send_default_pii": False,
        "integrations": [LoggingIntegration(level=logging.INFO, event_level=None)],
        # Passed explicitly: the SDK reads os.environ only, so a value from
        # a .env file would otherwise be lost.
        "environment": config("SENTRY_ENVIRONMENT", default=settings.env),
        # A None release keeps the SDK detection (git, CI values).
        "release": config("SENTRY_RELEASE", default=None),
    }
    sentry_sdk.init(dsn=dsn, **(defaults | init_kwargs))


class SentryConfiguration(TelemetryConfiguration):
    """The built-in Sentry backend: active when SENTRY_DSN is set or a host
    client is active. Override get_handler to customize."""

    def get_handler(self, orchestrator_config):
        # A host application may have registered through setup_sentry
        # already; a second handler would report every error twice.
        if get_registered_handler(SentryTelemetryHandler) is not None:
            return None
        dsn = config("SENTRY_DSN", default=None)
        if dsn is None and not sentry_sdk.get_client().is_active():
            return None
        _init_sdk_if_needed(dsn, {})
        return SentryTelemetryHandler()

    def shutdown(self):
        # Flush, do not close: with an active client owned by a host
        # application, the client must survive the run.
        sentry_sdk.get_client().flush()


def setup_sentry(dsn=None, **init_kwargs):
    """Initialize the SDK if needed and register the handler, for a host
    application that embeds winslow. Idempotent: a second call returns the
    handler that is already registered."""
    registered = get_registered_handler(SentryTelemetryHandler)
    if registered is not None:
        return registered
    _init_sdk_if_needed(dsn, init_kwargs)
    handler = SentryTelemetryHandler()
    register_error_handler(handler)
    return handler
