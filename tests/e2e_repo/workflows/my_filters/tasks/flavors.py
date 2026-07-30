from target_base import TargetTask


class Sweet(TargetTask):
    groups = "mild"
    flavor = "sweet"
    tags = ("dessert",)


class Sour(TargetTask):
    groups = "mild"
    flavor = "sour"


class Salty(TargetTask):
    groups = "strong"
    flavor = "salty"


class Bitter(TargetTask):
    groups = "strong"
    flavor = "bitter"
