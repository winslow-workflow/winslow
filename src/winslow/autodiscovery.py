import importlib
import logging
import pkgutil
import tomllib
from functools import lru_cache
from importlib.metadata import entry_points
from pathlib import Path

from winslow.util import classes_in_module
from winslow.exceptions import PluginError
from winslow._base import derive_name

logger = logging.getLogger(__name__)


@lru_cache
def _winslow_pyproject_table(cwd):
    """The [tool.winslow] table from `cwd/pyproject.toml`. Returns {} if the file
    does not exist or cannot be read.

    The result is cached per directory, so the file is read one time per process.
    Without the cache it is read for each registry, and each Workflow.__init__
    and each UI screen builds one.

    A file that does not exist is normal. A file that is malformed, because an
    edit is not complete or because it holds a git merge-conflict marker, belongs
    to the user and not to winslow. This function therefore gives a warning and
    continues with no config. A raw TOMLDecodeError would stop each command: show,
    a headless run and the construction of a TUI screen."""
    path = cwd / "pyproject.toml"
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning(
            "Could not read %s (%s); ignoring any winslow config in it.", path, e
        )
        return {}
    return data.get("tool", {}).get("winslow", {})


def _load_winslow_config(config_key):
    winslow = _winslow_pyproject_table(Path.cwd())
    disabled = set(winslow.get(f"disabled_{config_key}", []))
    enabled = set(winslow.get(f"enabled_{config_key}", []))
    clash = disabled & enabled
    if clash:
        raise PluginError(
            f"Entries appear in both disabled_{config_key} and enabled_{config_key}: {clash}"
        )
    return disabled, enabled


def _iter_package_modules(package):
    for _, modname, _ in pkgutil.walk_packages(
        package.__path__, prefix=package.__name__ + "."
    ):
        yield importlib.import_module(modname)


class Registerable:
    """Mixin for a class that autodiscovery can find and register."""

    autoload = True
    name = None  # optional explicit name. The default is the kebab-cased class name

    @classmethod
    @lru_cache
    def get_name(cls):
        return derive_name(cls)

    @classmethod
    def _qualified_name(cls, source):
        return f"{source}.{cls.get_name()}"

    @classmethod
    def _should_load(cls, source, enabled):
        # autoload=True: always load the class.
        # autoload=False: load the class only if enabled_<key> has an entry for
        # it. The entry is the qualified name or the full source dist.
        return (
            cls.autoload or cls._qualified_name(source) in enabled or source in enabled
        )


class BaseRegistry:
    """
    Base for an autodiscovering registry.

    A subclass must set:
        _config_key        - the key suffix in pyproject.toml, e.g. "tui_plugins"
        _entry_point_group - the entry point group, e.g. "winslow.tui_plugins"
        _base_class        - the Registerable subclass to collect

    A subclass must implement:
        _do_register(cls, source) - the action for a class that passes each guard
    """

    _config_key = None
    _entry_point_group = None
    _base_class = None

    def __init__(self):
        self._disabled, self._enabled = _load_winslow_config(self._config_key)

    def _is_candidate(self, cls):
        """An additional filter after isinstance. Override it to add a
        class-level requirement."""
        return True

    def _is_allowed(self, cls, source):
        qname = cls._qualified_name(source)
        return qname not in self._disabled and not (
            source in self._disabled and qname not in self._enabled
        )

    def _do_register(self, cls, source):
        raise NotImplementedError

    def _register(self, cls, source):
        if not self._is_candidate(cls):
            return
        if not cls._should_load(source, self._enabled):
            return
        if not self._is_allowed(cls, source):
            return
        self._do_register(cls, source)

    def _register_package(self, package, source):
        seen = set()
        for module in _iter_package_modules(package):
            for cls in classes_in_module(module, self._base_class):
                if cls not in seen:
                    seen.add(cls)
                    self._register(cls, source)

    def discover(self, package):
        self._register_package(package, source="builtin")

    def discover_installed(self):
        for ep in entry_points(group=self._entry_point_group):
            self._register_package(ep.load(), source=ep.dist.name)
