from enum import Enum, auto


class ParameterStyle(Enum):
    SEQUENTIAL = auto()
    PRODUCT = auto()


# The endpoints a serve process can open (see ServeApp). web is the browser
# UI at /, which talks to ws.
ENDPOINTS = ("ws", "web", "mcp")
DEFAULT_ENDPOINTS = ("ws", "web")

# The seconds a stopping serve process gives its open connections, then its
# running batches (see ServeApp.drain). The shutdown ends the client streams
# first (see ServeApp.close_streams), so the bound covers a client that does not read.
SHUTDOWN_GRACE = 5


class Mode(Enum):
    TUI = "tui"
    HEADLESS = "headless"

    def __str__(self):
        # argparse shows the choices with str(), so --help shows {tui,headless}.
        return self.value


# The hosts of a loopback bind: every process on the machine reaches it.
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
