"""The builtin filters, driven through the real --filter CLI path - no plugin
mocking. Baseline that the CLI -> filter -> filtered headless run works before
the discovery tests layer custom filters on top."""

import pytest

from winslow.exceptions import MisconfigurationError


def test_name_filter_runs_only_matches(build_filtered, assert_only_ran):
    workflow = build_filtered("--filter", "Sweet")
    workflow.headless_run()
    assert_only_ran(workflow, ran={"Sweet"})


def test_group_filter_runs_only_group(build_filtered, assert_only_ran):
    workflow = build_filtered("--filter", "!g strong")
    workflow.headless_run()
    assert_only_ran(workflow, ran={"Salty", "Bitter"})


def test_empty_match_is_an_error(build_filtered):
    workflow = build_filtered("--filter", "!g nonexistent")
    with pytest.raises(MisconfigurationError):
        workflow.headless_run()
