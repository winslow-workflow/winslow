import pytest
from argparse import Namespace

from winslow import ClassConstraint, Constraint, ConstraintType, Task
from winslow.exceptions import MisconfigurationError


class SingleType(Constraint):
    valid_types = ConstraintType.ELIGIBILITY

    def apply(self, task):
        return True


class ManyTypes(Constraint):
    valid_types = {ConstraintType.ELIGIBILITY, ConstraintType.RUNNABILITY}

    def apply(self, task):
        return True


class Accepts(Task):
    eligibility_constraints = [SingleType]

    def check(self):
        return True


class Rejects(Task):
    runnability_constraints = [SingleType]

    def check(self):
        return True


class Collects(Task):
    runnability_constraints = [ManyTypes]

    def check(self):
        return True


def test_a_single_type_gates_its_own_list():
    assert Accepts(Namespace())._evaluate_is_eligible() is True


def test_a_single_type_is_rejected_in_another_list():
    with pytest.raises(MisconfigurationError):
        Rejects(Namespace())._evaluate_can_run()


def test_a_collection_still_works():
    assert Collects(Namespace())._evaluate_can_run() is True


def test_get_valid_types_normalizes_to_a_frozenset():
    assert SingleType.get_valid_types() == frozenset({ConstraintType.ELIGIBILITY})
    assert Constraint.get_valid_types() == frozenset()


class DenyGate(Constraint):
    def apply(self, task):
        return False


class DenyInit(ClassConstraint):
    def apply(self, task_class, parameters=None):
        return False


class Bare(Task):
    eligibility_constraints = DenyGate

    def check(self):
        return True


class BareInit(Task):
    initialization_constraints = DenyInit

    def check(self):
        return True


def test_a_bare_constraint_gates_the_task():
    assert Bare(Namespace())._evaluate_is_eligible() is False


def test_a_bare_class_constraint_gates_the_initialization():
    assert BareInit._evaluate_should_be_initialized(Namespace()) is False
