"""One registry per store kind: the backends a WINSLOW_*_BACKEND setting
selects among. A package registers another backend at import time, before
a process builds its store (see winslow.state, winslow.access)."""

from winslow import settings
from winslow.exceptions import MisconfigurationError


class BackendRegistry:
    """The backends of one store under the names a setting selects by. name
    is the registry as a package addresses it, setting the environment
    variable, read when create runs."""

    def __init__(self, name, setting, **backends):
        self.name = name
        self.setting = setting
        self.backends = dict(backends)

    def register(self, name, store_class):
        self.backends[name] = store_class

    def create(self, *args):
        """The store the setting selects, built with args."""
        selected = getattr(settings, self.setting.removeprefix("WINSLOW_"))
        if selected not in self.backends:
            raise MisconfigurationError(
                f"{self.setting}={selected!r} names no registered backend. The "
                f"registered backends are {sorted(self.backends)}. A package "
                f"adds one with {self.name}.register()."
            )
        return self.backends[selected](*args)
