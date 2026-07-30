"""An autoloaded filter can be turned off via [tool.winslow] disabled_filter_plugins -
by qualified name or by whole source dist - so the command it registered
becomes unknown."""

import pytest

from winslow.exceptions import MisconfigurationError


def test_disabled_by_qualified_name(install_filters, build_filtered):
    install_filters(tool_winslow={"disabled_filter_plugins": ["acme-filters.flavor"]})
    workflow = build_filtered("--filter", "!flavor sweet")
    with pytest.raises(MisconfigurationError):
        workflow.headless_run()


def test_disabled_by_source(install_filters, build_filtered):
    install_filters(tool_winslow={"disabled_filter_plugins": ["acme-filters"]})
    workflow = build_filtered("--filter", "!flavor sweet")
    with pytest.raises(MisconfigurationError):
        workflow.headless_run()
