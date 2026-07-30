"""An autoload=False filter is discovered but not registered unless opted in
via [tool.winslow] enabled_filter_plugins - by qualified name or by whole source dist."""

import pytest

from winslow.exceptions import MisconfigurationError


def test_optin_filter_absent_by_default(install_filters, build_filtered):
    install_filters()
    workflow = build_filtered("--filter", "!tag dessert")
    with pytest.raises(MisconfigurationError):
        workflow.headless_run()


def test_optin_filter_enabled_by_qualified_name(
    install_filters, build_filtered, assert_only_ran
):
    install_filters(tool_winslow={"enabled_filter_plugins": ["acme-filters.tag"]})
    workflow = build_filtered("--filter", "!tag dessert")
    workflow.headless_run()
    assert_only_ran(workflow, ran={"Sweet"})


def test_optin_filter_enabled_by_source(
    install_filters, build_filtered, assert_only_ran
):
    install_filters(tool_winslow={"enabled_filter_plugins": ["acme-filters"]})
    workflow = build_filtered("--filter", "!tag dessert")
    workflow.headless_run()
    assert_only_ran(workflow, ran={"Sweet"})
