from winslow import Parameter

from target_base import TargetTask


class Ship(TargetTask):
    """One instance per region. The class is in the group "ship", and each
    instance is in the group of its own region, for example "ship-eu"."""

    region = Parameter(values=("eu", "us", "ap"))

    @classmethod
    def get_groups(cls, parameters=None):
        if parameters is None:
            return frozenset({"ship"})
        return frozenset({f"ship-{parameters.region}"})


class Pack(TargetTask):
    """Declares its group and overrides nothing."""

    groups = "prepare"


class Notify(TargetTask):
    """Depends on the class group, so on every Ship instance."""

    dependencies = "ship"
