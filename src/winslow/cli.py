import sys
from winslow.orchestrator import Orchestrator
from winslow.exceptions import WinslowError
from winslow.logger import LOGGER, initialize_logging


def main():
    """The entry point of the CLI."""
    orchestrator_klass = Orchestrator.get_from_cwd()

    if not orchestrator_klass:
        LOGGER.debug(
            "No explicit orchestrator declaration found - using default Orchestrator."
        )

    kls = orchestrator_klass or Orchestrator

    base_args, unknown = kls.parse_args()

    initialize_logging(debug_mode=getattr(base_args, "debug", False))

    orchestrator = kls(base_args, unknown_args=unknown)
    try:
        result = orchestrator.start()
    except WinslowError as e:
        # A config or setup error, for example a malformed --filter, gives a
        # clean message and a non-zero exit, and not a traceback.
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # A simple run reports the task outcomes. False means that some tasks were
    # not successful. Each other mode returns None and exits with 0.
    if result is False:
        sys.exit(1)


if __name__ == "__main__":
    main()
