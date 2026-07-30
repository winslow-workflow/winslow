import os
import re
import sys
from functools import cached_property
from argparse import Namespace

from winslow import settings
from winslow.util import camel_to_kebab
from winslow.logger import LOGGER
from winslow.exceptions import MisconfigurationError


# A name is a technical identifier. It goes into a logger name, which uses dots,
# and also into a file name, a session id, a Loki label, a URL and a CLI arg. The
# charset is thus limited to a safe slug, so each of those stays valid: no "."
# (the logger separator), no "/" (a path) and no whitespace. For a label for a
# person, or for i18n, use display_name.
_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def derive_name(cls):
    """Return the explicit `name` of the class, or the kebab-cased class name, and
    validate its charset. A plugin name and a filter name thus get the same test
    as a workflow name and a task name."""
    name = getattr(cls, "name", None)
    if name is None:
        name = camel_to_kebab(cls.__name__)
    elif not isinstance(cls.name, str) or not cls.name:
        raise MisconfigurationError(
            f"Invalid name definition for {cls} ({name}) - needs to be a non-empty string."
        )
    if not _NAME_PATTERN.match(name):
        raise MisconfigurationError(
            f"Invalid name '{name}' for {cls}: must match [A-Za-z0-9_-]+ "
            f"(no spaces, dots, slashes or other special characters)."
        )
    return name


class _Base:
    """Base class for the components."""

    # An optional label for a person. The format is free and i18n is possible. It
    # is for display only and is never used in a technical path. get_name() is
    # used if this is not set.
    display_name = None

    def __init__(self, orchestrator_config: Namespace):
        self.orchestrator_config = orchestrator_config
        LOGGER.debug(f"{self} initialized with config {self.orchestrator_config}")

    def __str__(self):
        return self.instance_name

    @property
    def env(self) -> str:
        return settings.env

    @classmethod
    def get_module_path(cls):
        return cls.__module__.replace(".", os.sep)

    @property
    def abspath(self):
        return os.path.abspath(sys.modules[self.__class__.__module__].__file__)

    @property
    def module_directory(self):
        return os.path.dirname(self.abspath)

    @cached_property
    def instance_name(self):
        return self.__class__.get_name()

    @classmethod
    def get_name(cls):
        return derive_name(cls)

    @classmethod
    def get_display_name(cls):
        """The label for a person. The format is free and i18n is possible.
        Returns get_name() if display_name is not set."""
        display_name = getattr(cls, "display_name", None)
        if display_name is None:
            return cls.get_name()
        if not isinstance(display_name, str) or not display_name:
            raise MisconfigurationError(
                f"Invalid display_name for {cls} ({display_name!r}) - Needs to be a non-empty string."
            )
        return display_name
