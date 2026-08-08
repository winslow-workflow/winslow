from target_base import TargetTask


class Boom(TargetTask):
    """A defect on an identified run: the telemetry context must say which
    configured run it happened in."""

    def run(self):
        raise RuntimeError("boom")
