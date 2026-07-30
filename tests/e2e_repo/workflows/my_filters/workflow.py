from target_base import TargetWorkflow


class MyFilters(TargetWorkflow):
    """The filter fixture: tasks distinguishable by name, group, a custom
    `flavor` attribute, and `tags` - so builtin and custom filters each have
    something to select on in a headless run."""
