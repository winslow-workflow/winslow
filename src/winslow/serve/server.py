"""The uvicorn server of a serve process (see ServeApp). Requires the [serve] extra."""

import uvicorn
from uvicorn.config import STARTUP_FAILURE


class HttpServer(uvicorn.Server):
    """The uvicorn server of one ServeApp. The shutdown ends the client
    streams before uvicorn waits for the connections. An open stream holds
    its connection until the client leaves, and uvicorn cancels that request
    at the graceful timeout with an error log."""

    def __init__(self, serve_app, **config):
        super().__init__(uvicorn.Config(serve_app.starlette(), **config))
        self.serve_app = serve_app

    def run(self, sockets=None):
        try:
            super().run(sockets)
        except KeyboardInterrupt:
            # uvicorn raises the captured Ctrl+C again once the loop has stopped.
            pass
        # uvicorn logs the bind error itself. The exit code is the one uvicorn.run uses.
        if not self.started:
            raise SystemExit(STARTUP_FAILURE)

    async def shutdown(self, sockets=None):
        self.serve_app.close_streams()
        await super().shutdown(sockets)
