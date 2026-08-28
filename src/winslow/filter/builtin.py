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


class _BuiltinResolver:
    """A registry stand-in that resolves only the builtin filter commands.
    FilterParser reads `default` and `resolve` (see FilterRegistry)."""

    default = NameFilter

    @classmethod
    def resolve(cls, cmd):
        for filter_cls in BUILTIN_FILTERS:
            if cmd in (filter_cls.short_command, filter_cls.long_command):
                return filter_cls
        raise ValueError(
            f"'!{cmd}' is not a builtin filter - this search supports only "
            f"name and group."
        )


def parse_builtin(query_string):
    """Parse a query with the builtin filters alone. A client-side search
    over stored rows uses this: it needs no live session and no project
    filter code. Raises ValueError with the parse error."""
    from .parser import FilterParser

    return FilterParser(_BuiltinResolver).parse(query_string)


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
