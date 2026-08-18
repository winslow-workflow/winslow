import logging

from functools import cached_property

import pytest

from winslow.cache import WorkflowCache, entry, validate_cache_class
from winslow.exceptions import MisconfigurationError


def validate_error(cache_class, match):
    with pytest.raises(MisconfigurationError, match=match):
        validate_cache_class(cache_class)


def test_entry_rejects_options_next_to_a_positional_func():
    """entry(loader, eager=True) built a lazy entry and dropped the options
    silently - it must refuse instead."""
    with pytest.raises(MisconfigurationError, match="drops the options"):
        entry(lambda self: 1, eager=True)
    with pytest.raises(MisconfigurationError, match="drops the options"):
        entry(lambda self: 1, ttl=30)


def test_a_valid_class_passes():
    class Valid(WorkflowCache):
        @entry(eager=True, ttl=60)
        def base(self):
            return 1

        @entry(eager=True, depends_on="base")
        def derived(self):
            return self.base + 1

        @entry(depends_on=("base", "derived"))
        def lazy_view(self):
            return (self.base, self.derived)

    validate_cache_class(Valid)


def test_default_name_is_the_snake_cased_class_name():
    class MyDbCache(WorkflowCache):
        pass

    assert MyDbCache.get_name() == "my_db_cache"


def test_name_override_and_charset():
    class Named(WorkflowCache):
        name = "foo_bar_cache"

    assert Named.get_name() == "foo_bar_cache"

    class Dashed(WorkflowCache):
        name = "foo-bar"

    with pytest.raises(MisconfigurationError, match=r"\[a-z\]\[a-z0-9_\]\*"):
        Dashed.get_name()

    # An underscore name would be reachable on the container but names a
    # private-looking attribute; the pattern rejects it at collection.
    class Underscored(WorkflowCache):
        name = "_foo"

    with pytest.raises(MisconfigurationError, match=r"\[a-z\]\[a-z0-9_\]\*"):
        Underscored.get_name()


def test_display_style_is_a_style_or_a_formatter():
    from winslow.cache import DisplayStyle

    class Styled(WorkflowCache):
        @entry(display_style=DisplayStyle.TREE)
        def shaped(self):
            return {}

        @entry(display_style=lambda value: str(value))
        def formatted(self):
            return {}

    validate_cache_class(Styled)

    class Wrong(WorkflowCache):
        @entry(display_style="tree")
        def value(self):
            return {}

    validate_error(Wrong, "DisplayStyle member")

    class TwoArgs(WorkflowCache):
        @entry(display_style=lambda value, extra: str(value))
        def value(self):
            return {}

    validate_error(TwoArgs, "exactly the value")


def test_loader_must_take_exactly_self():
    class Extra(WorkflowCache):
        @entry
        def value(self, when):
            return when

    validate_error(Extra, "exactly 'self'")


def test_ttl_must_be_a_positive_number():
    def build(ttl_value):
        class Timed(WorkflowCache):
            @entry(ttl=ttl_value)
            def value(self):
                return 1

        return Timed

    for bad in (0, -5, "10", True):
        validate_error(build(bad), "ttl must be a positive number")
    validate_cache_class(build(0.5))


def test_depends_on_must_name_an_entry_of_the_class():
    class Unknown(WorkflowCache):
        @entry(eager=True, depends_on="nope")
        def value(self):
            return 1

    validate_error(Unknown, "not an @entry of this class")


def test_an_eager_entry_cannot_depend_on_a_lazy_one():
    class EagerOnLazy(WorkflowCache):
        @entry
        def lazy_base(self):
            return 1

        @entry(eager=True, depends_on="lazy_base")
        def eager_view(self):
            return self.lazy_base

    validate_error(EagerOnLazy, "cannot depend on the lazy entry")


def test_a_lazy_entry_may_depend_on_any_entry():
    class LazyDeps(WorkflowCache):
        @entry(eager=True)
        def eager_base(self):
            return 1

        @entry
        def lazy_base(self):
            return 2

        @entry(depends_on=("eager_base", "lazy_base"))
        def view(self):
            return (self.eager_base, self.lazy_base)

    validate_cache_class(LazyDeps)


def test_a_dependency_cycle_is_rejected():
    class Cycle(WorkflowCache):
        @entry(eager=True, depends_on="b")
        def a(self):
            return 1

        @entry(eager=True, depends_on="a")
        def b(self):
            return 2

    validate_error(Cycle, "cyclical entry dependency")


def test_plain_cached_property_is_rejected():
    class Mixed(WorkflowCache):
        @cached_property
        def value(self):
            return 1

    validate_error(Mixed, "functools.cached_property")


def test_underscore_entry_is_rejected():
    class Hidden(WorkflowCache):
        @entry
        def _value(self):
            return 1

    validate_error(Hidden, "must not start with an underscore")


def test_an_entry_cannot_shadow_the_cache_api():
    class Shadow(WorkflowCache):
        @entry
        def invalidate(self):
            return 1

    validate_error(Shadow, "shadows a non-entry member")


def test_an_entry_cannot_shadow_a_scope_config_attribute():
    """workflow_config and orchestrator_config are instance attributes, which
    a vars() scan of the MRO cannot see - they are reserved explicitly."""

    class Shadow(WorkflowCache):
        @entry
        def workflow_config(self):
            return 1

    validate_error(Shadow, "shadows a non-entry member")


def test_an_entry_cannot_shadow_a_project_helper():
    """The reserve covers the whole MRO: a helper on a project's own cache
    base class is protected the same way as the framework API."""

    class ProjectBase(WorkflowCache):
        class Meta:
            abstract = True

        def helper(self):
            return 1

    class Leaf(ProjectBase):
        @entry
        def helper(self):
            return 2

    validate_error(Leaf, "shadows a non-entry member")


def test_an_entry_can_override_an_entry_of_a_base():
    class Base(WorkflowCache):
        class Meta:
            abstract = True

        @entry
        def value(self):
            return 1

    class Leaf(Base):
        @entry
        def value(self):
            return 2

    validate_cache_class(Leaf)  # no error: both declarations are entries


def test_ttl_mismatch_logs_a_warning(caplog):
    class Mismatch(WorkflowCache):
        @entry(eager=True, ttl=10)
        def fast(self):
            return 1

        @entry(eager=True, depends_on="fast")
        def forever(self):
            return self.fast

        @entry(eager=True, depends_on="fast", ttl=60)
        def slow(self):
            return self.fast

        @entry(eager=True, depends_on="fast", ttl=5)
        def faster(self):
            return self.fast

    with caplog.at_level(logging.WARNING, logger="winslow"):
        validate_cache_class(Mismatch)

    warned = sorted(record.getMessage().split(" ")[0] for record in caplog.records)
    assert warned == ["Mismatch.forever", "Mismatch.slow"]
