from target_base import TargetTask


class Gated(TargetTask):
    """Holds its batch open until the test releases the gate event parked in
    the target - how lifecycle tests create a deterministically busy session.
    The timeout keeps a broken test from hanging the whole suite."""

    def run(self):
        self.target[("gate",)].wait(timeout=30)
        super().run()


class TailOne(TargetTask):
    """Sorts after Gated, so under --disable-concurrency it queues behind the
    held gate - the task a stop sweep catches before it ever starts."""


class TailTwo(TargetTask):
    """Second sweep victim, same reason as TailOne."""
