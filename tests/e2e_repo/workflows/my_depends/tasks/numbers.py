from winslow import Parameter

from target_base import TargetTask


class Producer(TargetTask):
    """One instance per number. A poisoned instance runs but writes nothing,
    so its post-run check fails - how tests fail one member of the family."""

    number = Parameter(values=tuple(range(1, 11)))

    def run(self):
        if ("poison", self.number) in self.target:
            return
        super().run()


class OddConsumer(TargetTask):
    """depends_on narrows the class-level Producer dependency down to the
    instance with the matching number - one real dependency, not ten."""

    number = Parameter(values=(1, 3, 5, 7, 9))
    dependencies = Producer

    def depends_on(self, task):
        return self.number == task.number


class EvenConsumer(TargetTask):
    """Even counterpart of OddConsumer."""

    number = Parameter(values=(2, 4, 6, 8, 10))
    dependencies = Producer

    def depends_on(self, task):
        return self.number == task.number


class FanIn(TargetTask):
    """No depends_on override - the default keeps every Producer instance, so
    this single task depends on all ten."""

    dependencies = Producer
