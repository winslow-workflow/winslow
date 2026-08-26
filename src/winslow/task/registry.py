import collections
from functools import cached_property

from winslow.registry import Registry
from .task import Task


class TaskRegistry(Registry):
    item_class = Task

    def __init__(self, orchestrator_config, workflow_config):
        super().__init__(orchestrator_config)
        self.workflow_config = workflow_config
        self._group_registry = collections.defaultdict(set)

    @cached_property
    def premier_task_classes(self):
        return {kls for kls in self._name_registry.values() if kls.is_premier}

    def get_registration_context(self):
        return super().get_registration_context() + [
            (self._group_registry, lambda task: task.get_groups(), False)
        ]

    def filter_by_string(self, lookup):
        result = set()

        for source, _, is_strict in self.get_registration_context():
            if lookup not in source:
                continue

            value = source[lookup]

            if is_strict:
                result.add(value)
            else:
                result.update(value)

        return result
