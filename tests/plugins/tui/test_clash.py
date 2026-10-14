"""Registration-time failures: two plugins with one name, from a package or
from the list beside a built-in."""

import pytest

from winslow.exceptions import PluginError

from .conftest import ALPHA


def test_duplicate_name_raises(make_registry, clash_package):
    reg = make_registry()
    with pytest.raises(PluginError, match="name clash: dup"):
        reg.register_package(clash_package)


def test_a_listed_plugin_with_the_name_of_a_registered_one_raises(
    make_registry, plugins_package
):
    reg = make_registry()
    reg.register_package(plugins_package)
    with pytest.raises(PluginError, match="name clash: alpha"):
        reg.load([ALPHA])
