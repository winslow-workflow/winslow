from target_base import TargetTask


class SlowProducer(TargetTask):
    """Holds its batch open until the test releases its gate, then lands its
    work - the ACTIVE dependency a sibling batch's consumer must wait out."""

    def run(self):
        self.target[("gate", self)].wait(timeout=30)
        super().run()


class SlowFailer(TargetTask):
    """Same held gate, but the run lands nothing - so the waiting consumer
    sees the dependency settle on a failed check, not on success."""

    def run(self):
        self.target[("gate", self)].wait(timeout=30)


class NeedsProducer(TargetTask):
    dependencies = SlowProducer


class NeedsFailer(TargetTask):
    dependencies = SlowFailer
