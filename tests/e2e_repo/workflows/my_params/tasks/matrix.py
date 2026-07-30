from winslow import Parameter

from target_base import TargetTask


class Deploy(TargetTask):
    """Plain value parameter - one instance per region (3 tasks)."""

    region = Parameter(values=("eu", "us", "ap"))


class Matrix(TargetTask):
    """Compound parameter - one instance per (region, tier) row (2 tasks)."""

    region_tier = Parameter.from_tuple(
        [
            ("eu", "free"),
            ("us", "paid"),
        ]
    )


class Collect(TargetTask):
    """Depends on the whole Deploy family (every instance)."""

    dependencies = Deploy
