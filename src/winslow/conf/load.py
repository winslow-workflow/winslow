"""The loader of the settings: the base of winslow.conf.defaults under the
project module WINSLOW_SETTINGS_MODULE names, Django style. The project module
is free form: it overrides names of the base and adds its own."""

import importlib
import os
import sys
from dataclasses import dataclass

from winslow.conf import defaults
from winslow.exceptions import MisconfigurationError

MODULE_VARIABLE = "WINSLOW_SETTINGS_MODULE"
# The module a project keeps beside its workflows directory, picked up from
# the working directory the way the workflows are, when the variable is unset.
MODULE_FILE = "settings.py"


@dataclass(frozen=True)
class Loaded:
    """The two layers of the settings: the base, and the names the project
    module set. module is the name of that module, None without one."""

    base: dict
    overrides: dict
    module: str | None

    @property
    def names(self):
        return {**self.base, **self.overrides}

    def source(self, name):
        """`module` for a name the project module set, else `default`."""
        return "module" if name in self.overrides else "default"


def module_name():
    """The project module: the one the environment names, else `settings`
    when settings.py is in the working directory, else None."""
    named = os.environ.get(MODULE_VARIABLE)
    if named:
        return named
    if not os.path.isfile(os.path.join(os.getcwd(), MODULE_FILE)):
        return None
    # An embedding process may run without the working directory on the path.
    if os.getcwd() not in sys.path:
        sys.path.append(os.getcwd())
    return "settings"


def load(name=None):
    """The settings of one process: the base, then every upper-case name of
    the project module `name` (see Loaded)."""
    base = {key: value for key, value in vars(defaults).items() if key.isupper()}
    if name is None:
        return Loaded(base=base, overrides={}, module=None)
    try:
        module = importlib.import_module(name)
    except Exception as exc:
        raise MisconfigurationError(
            f"{module_origin(name)} does not import - {type(exc).__name__}: {exc}"
        ) from exc
    overrides = {key: value for key, value in vars(module).items() if key.isupper()}
    return Loaded(base=base, overrides=overrides, module=name)


def module_origin(name):
    """Where the process was told the module: the variable, else settings.py
    in the working directory (see module_name)."""
    if os.environ.get(MODULE_VARIABLE):
        return f"{MODULE_VARIABLE}={name!r}"
    return f"{MODULE_FILE} in {os.getcwd()}"


def import_string(path):
    """The object a dotted path names: the last dot splits the module and the
    attribute. A failure names the path, which the settings module holds once."""
    module_path, separator, attribute = path.rpartition(".")
    if not separator or not module_path or not attribute or path.startswith("."):
        raise MisconfigurationError(
            f"{path!r} is not a dotted path - write <module>.<Name>."
        )
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise MisconfigurationError(f"{path!r} does not import - {exc}") from exc
    try:
        return getattr(module, attribute)
    except AttributeError:
        raise MisconfigurationError(f"{module_path} has no {attribute!r}.") from None
