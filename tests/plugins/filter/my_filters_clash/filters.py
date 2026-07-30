"""Two autoloaded filters that share a command - discovering this package
registers both and forces the duplicate-command PluginError. Kept out of the
main my_filters package so it only bites the clash test."""

from winslow.filter.base import TaskFilter


class DupOne(TaskFilter):
    short_command = "dup"

    def matches(self, task):
        return False

    def explain(self):
        return "one"


class DupTwo(TaskFilter):
    short_command = "dup"

    def matches(self, task):
        return False

    def explain(self):
        return "two"
