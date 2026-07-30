from winslow.autodiscovery import BaseRegistry
from winslow.exceptions import PluginError

from .base import TaskFilter
from .builtin import NameFilter, GroupFilter
from .parser import FilterParser
from .query import FilterQuery


class FilterRegistry(BaseRegistry):
    _config_key = "filter_plugins"
    _entry_point_group = "winslow.filter_plugins"
    _base_class = TaskFilter

    def __init__(self, orchestrator_config, workflow_config):
        super().__init__()
        self.orchestrator_config = orchestrator_config
        self.workflow_config = workflow_config
        self._short = {}
        self._long = {}
        self._default = NameFilter
        self._register(GroupFilter, source="builtin")
        self.discover_installed()

    def _do_register(self, cls, source):
        if cls.short_command:
            existing = self._short.get(cls.short_command)
            if existing and existing is not cls:
                raise PluginError(
                    f"Filter command '!{cls.short_command}' already registered by {existing.__name__}"
                )
            self._short[cls.short_command] = cls
        if cls.long_command:
            existing = self._long.get(cls.long_command)
            if existing and existing is not cls:
                raise PluginError(
                    f"Filter command '!{cls.long_command}' already registered by {existing.__name__}"
                )
            self._long[cls.long_command] = cls

    def resolve(self, cmd):
        if cmd in self._short:
            return self._short[cmd]
        if cmd in self._long:
            return self._long[cmd]
        available = sorted(self._short.keys() | self._long.keys())
        raise ValueError(f"Unknown filter command '!{cmd}' - available: {available}")

    @property
    def default(self):
        return self._default

    def parse(self, query_string) -> FilterQuery:
        return FilterParser(self).parse(query_string)
