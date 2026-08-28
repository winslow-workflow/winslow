"""The one read boundary of the UI: a port read that fails answers the
default and a toast, never the crash screen. ConnectionError and
TimeoutError mark a wire outage - the transport reconnects on its own (see
winslow.client.websocket) - and RequestError is a served refusal (see
winslow.client.base). A repaint then skips one pass and the next event or
tick repaints from live state."""

from winslow.exceptions import RequestError

READ_FAILURES = (ConnectionError, TimeoutError, RequestError)


def port_read(widget, read, *args, default=None, quiet=False, **kwargs):
    """One port read for a UI handler. On a read failure the widget shows
    the reason and the caller receives `default`. An interval poll passes
    quiet=True, so an outage does not toast once per tick."""
    try:
        return read(*args, **kwargs)
    except READ_FAILURES as exc:
        if not quiet:
            widget.notify(str(exc), severity="warning")
        return default
