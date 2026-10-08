from enum import Enum, auto


class ParameterStyle(Enum):
    SEQUENTIAL = auto()
    PRODUCT = auto()


# The endpoints a serve process can open (see ServeApp). web is the browser
# UI at /, which talks to ws.
ENDPOINTS = ("ws", "web", "mcp")
DEFAULT_ENDPOINTS = ("ws", "web")

# The seconds a stopping serve process gives its open connections, then its
# running batches (see ServeApp.drain). An MCP client holds a stream open for
# as long as it is connected, so the connection wait needs a bound.
SHUTDOWN_GRACE = 5


class Mode(Enum):
    TUI = "tui"
    HEADLESS = "headless"

    def __str__(self):
        # argparse shows the choices with str(), so --help shows {tui,headless}.
        return self.value
