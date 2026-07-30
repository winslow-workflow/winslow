from enum import Enum, auto


class ParameterStyle(Enum):
    SEQUENTIAL = auto()
    PRODUCT = auto()


class Mode(Enum):
    TUI = "tui"
    HEADLESS = "headless"

    def __str__(self):
        # argparse shows the choices with str(), so --help shows {tui,headless}.
        return self.value
