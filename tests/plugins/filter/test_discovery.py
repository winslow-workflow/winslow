"""A custom filter reaches a headless run by being listed: FILTER_PLUGINS is
the one registration path, so a pass here exercises load -> _do_register ->
resolve -> parse -> the filtered run, end to end."""

import pytest

from winslow.exceptions import MisconfigurationError

from .conftest import FLAVOR, TAG


def test_a_listed_filter_is_usable_by_long_command(
    list_filters, build_filtered, assert_only_ran
):
    list_filters(FLAVOR)
    workflow = build_filtered("--filter", "!flavor sweet")
    workflow.headless_run()
    assert_only_ran(workflow, ran={"Sweet"})


def test_a_listed_filter_is_usable_by_short_command(
    list_filters, build_filtered, assert_only_ran
):
    list_filters(FLAVOR)
    workflow = build_filtered("--filter", "!f salty")
    workflow.headless_run()
    assert_only_ran(workflow, ran={"Salty"})


def test_an_unlisted_filter_is_an_unknown_command(list_filters, build_filtered):
    list_filters(FLAVOR)
    workflow = build_filtered("--filter", "!tag dessert")
    with pytest.raises(MisconfigurationError):
        workflow.headless_run()


def test_the_second_listed_filter_is_usable_too(
    list_filters, build_filtered, assert_only_ran
):
    list_filters(FLAVOR, TAG)
    workflow = build_filtered("--filter", "!tag dessert")
    workflow.headless_run()
    assert_only_ran(workflow, ran={"Sweet"})
