from enum import Enum, auto


class ParameterStyle(Enum):
    SEQUENTIAL = auto()
    PRODUCT = auto()


# The endpoints a serve process can open (see ServeApp).
ENDPOINTS = ("ws", "mcp")


class Mode(Enum):
    TUI = "tui"
    HEADLESS = "headless"

    def __str__(self):
        # argparse shows the choices with str(), so --help shows {tui,headless}.
        return self.value
