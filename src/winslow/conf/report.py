"""The lines `winslow settings` prints: every setting with its value and its
source, then every path with the object it resolved to. A deployment checks
its wiring before it starts a server (see docs/settings.md)."""

from winslow.conf import import_string
from winslow.exceptions import MisconfigurationError

# The names that print as `set` or `unset`.
SECRETS = ("AUTH_TICKET_SECRET", "SENTRY_DSN", "OIDC_CLIENT_SECRET")
# The settings that hold one dotted path, and those that hold a list of paths.
PATHS = ("STATE_STORE",)
LISTS = ("AUTH_BACKENDS", "TELEMETRY", "TUI_PLUGINS", "FILTER_PLUGINS")


def _shown(name, value):
    if name in SECRETS:
        return "set" if value else "unset"
    return repr(value)


def _resolution(setting, path):
    """One line: the class the path names, or why it does not resolve."""
    try:
        resolved = import_string(path)
    except MisconfigurationError as exc:
        return f"{setting}: {path} -> {exc}"
    return f"{setting} -> {resolved.__module__}.{resolved.__name__}"


def lines(settings):
    """The report of one Settings object (see winslow.conf.settings)."""
    loaded = settings.loaded
    yield f"settings module: {loaded.module or 'none'}"
    for name in sorted(loaded.names):
        value = getattr(settings, name)
        yield f"{name} = {_shown(name, value)}  [{loaded.source(name)}]"
    yield ""
    for setting in PATHS:
        yield _resolution(setting, getattr(settings, setting))
    for setting in LISTS:
        paths = tuple(getattr(settings, setting))
        if not paths:
            yield f"{setting} -> nothing"
        for path in paths:
            yield _resolution(setting, path)
