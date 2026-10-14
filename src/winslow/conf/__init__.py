"""The settings of the process, one object: `from winslow.conf import settings`
and `settings.ENV`. The first attribute read loads the base and the project
module WINSLOW_SETTINGS_MODULE names (see winslow.conf.load). No winslow module
reads a setting at import time, so the load runs after the import graph of the
process is complete.

A path setting holds a dotted string. The code that needs the object resolves
it where it is used: `import_string(settings.STATE_STORE)`."""

from winslow.conf.load import import_string, load, module_name
from winslow.exceptions import MisconfigurationError


class Settings:
    """The loaded settings as attributes. A read before the load triggers it,
    and the loaded names land on the object, so every later read is plain."""

    def __init__(self):
        self._loaded = None
        self._loading = False

    def _load(self):
        if self._loaded is None:
            if self._loading:
                raise MisconfigurationError(
                    f"{module_name()} reads a setting while the settings load - a "
                    f"settings module names code as strings and reads no setting."
                )
            self._loading = True
            try:
                loaded = load(module_name())
            finally:
                self._loading = False
            self._loaded = loaded
            vars(self).update(loaded.names)
        return self._loaded

    def __getattr__(self, name):
        # Only a miss reaches here: the first read, or a name no layer holds.
        if name.startswith("_"):
            raise AttributeError(name)
        loaded = self._load()
        if name in loaded.names:
            return loaded.names[name]
        raise AttributeError(f"the settings hold no {name!r}")

    @property
    def loaded(self):
        """The two layers, for `winslow settings` (see Loaded)."""
        return self._load()

    def env_color(self):
        """The header color of ENV, gray for a name the table does not hold."""
        return self.ENV_COLORS.get(self.ENV, "gray")


settings = Settings()

__all__ = ["Settings", "import_string", "load", "settings"]
