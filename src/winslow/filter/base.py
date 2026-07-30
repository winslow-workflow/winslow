from functools import lru_cache

from winslow.autodiscovery import Registerable


class TaskFilter(Registerable):
    short_command = None
    long_command = None

    def __init__(self, value):
        self.value = value

    @classmethod
    @lru_cache
    def get_name(cls):
        # Use long_command first, then short_command, then the kebab class name.
        return cls.long_command or cls.short_command or super().get_name()

    def matches(self, task) -> bool:
        raise NotImplementedError

    def explain(self) -> str:
        raise NotImplementedError
