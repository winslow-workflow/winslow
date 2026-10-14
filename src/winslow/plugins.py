"""The plugin registries. A plugin class registers when the TUI_PLUGINS or
FILTER_PLUGINS setting lists it by path, beside the built-ins of the package
that owns the registry (see winslow.conf, docs/plugins.md)."""

import importlib
import pkgutil
from functools import lru_cache

from winslow._base import derive_name
from winslow.conf import import_string
from winslow.exceptions import PluginError
from winslow.util import classes_in_module


def _iter_package_modules(package):
    for _, modname, _ in pkgutil.walk_packages(
        package.__path__, prefix=package.__name__ + "."
    ):
        yield importlib.import_module(modname)


class Registerable:
    """The base of a plugin class. name is the explicit name. The default is
    the kebab-cased class name."""

    name = None

    @classmethod
    @lru_cache
    def get_name(cls):
        return derive_name(cls)


class BaseRegistry:
    """The base of a plugin registry. A subclass sets _base_class, the
    Registerable kind it collects, and _setting, the list setting that names
    the plugins of a project, and implements _do_register."""

    _base_class = None
    _setting = None

    def _is_candidate(self, cls):
        """An additional filter after the kind. Override it to add a class
        level requirement."""
        return True

    def _do_register(self, cls):
        raise NotImplementedError

    def _register(self, cls):
        if self._is_candidate(cls):
            self._do_register(cls)

    def register_package(self, package):
        """Register every class of the kind the modules of the package
        define: the built-ins."""
        seen = set()
        for module in _iter_package_modules(package):
            for cls in classes_in_module(module, self._base_class):
                if cls not in seen:
                    seen.add(cls)
                    self._register(cls)

    def load(self, paths):
        """Register the classes the paths name, in order (see import_string)."""
        for path in paths:
            cls = import_string(path)
            if not isinstance(cls, type) or not issubclass(cls, self._base_class):
                raise PluginError(
                    f"{self._setting}: {path} is not a {self._base_class.__name__} class."
                )
            self._register(cls)
