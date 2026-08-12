from .base import TaskFilter


class NameFilter(TaskFilter):
    def matches(self, task):
        return self.value.lower() in task.get_name().lower()

    def explain(self):
        return f"name contains '{self.value}'"


class GroupFilter(TaskFilter):
    short_command = "g"
    long_command = "group"

    def matches(self, task):
        return self.value in task.get_groups()

    def explain(self):
        return f"in group '{self.value}'"


# The filters that the history search accepts: they read only get_name and
# get_groups, which TaskInfo also provides. A project filter needs a live task.
BUILTIN_FILTERS = (NameFilter, GroupFilter)
