"""Custom task filters for the registration tests: the tests list them in
FILTER_PLUGINS by path, and the registry loads them for real."""

from winslow.filter.base import TaskFilter


class FlavorFilter(TaskFilter):
    """A listed filter: matches on a task attribute the builtins do not know."""

    short_command = "f"
    long_command = "flavor"

    def matches(self, task):
        return getattr(task, "flavor", None) == self.value

    def explain(self):
        return f"flavor is '{self.value}'"


class TagFilter(TaskFilter):
    """A second filter, listed or not by the test at hand."""

    long_command = "tag"

    def matches(self, task):
        return self.value in getattr(task, "tags", ())

    def explain(self):
        return f"tagged '{self.value}'"
