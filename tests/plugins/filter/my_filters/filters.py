"""Custom task filters for the discovery/registrability e2e tests. Stands in
for an installed third-party package: the tests point a fake `winslow.filter_plugins`
entry point at this package, so `discover_installed` walks it for real.

Defined in a submodule (not the package __init__) because _iter_package_modules
walks submodules only."""

from winslow.filter.base import TaskFilter


class FlavorFilter(TaskFilter):
    """Autoloaded custom filter - the discovered-and-usable case. Matches on a
    task attribute the builtins don't know about."""

    short_command = "f"
    long_command = "flavor"

    def matches(self, task):
        return getattr(task, "flavor", None) == self.value

    def explain(self):
        return f"flavor is '{self.value}'"


class TagFilter(TaskFilter):
    """Opt-in (autoload=False): absent unless enabled_filter_plugins lists it."""

    autoload = False
    long_command = "tag"

    def matches(self, task):
        return self.value in getattr(task, "tags", ())

    def explain(self):
        return f"tagged '{self.value}'"
