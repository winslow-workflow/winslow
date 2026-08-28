"""The one read boundary of the UI: a failed port read answers a default
and a toast. ConnectionError and TimeoutError mark a wire outage, and the
transport reconnects on its own (see winslow.client.websocket);
RequestError is a served refusal (see winslow.client.base). The caller
skips one repaint, and the next event or tick repaints from live state."""

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
