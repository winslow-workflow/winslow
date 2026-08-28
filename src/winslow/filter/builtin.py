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


def enforce_builtin_only(query):
    """Raise ValueError when the parsed query uses a filter outside
    BUILTIN_FILTERS. The serve edge and the session port apply the same rule
    to a builtin-only search."""
    foreign = sorted(
        {
            type(f).get_name()
            for f in query.filters()
            if type(f) not in BUILTIN_FILTERS
        }
    )
    if foreign:
        raise ValueError(
            f"this search supports only the builtin filters (name, group) - "
            f"not: {', '.join(foreign)}."
        )
