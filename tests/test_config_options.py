import pytest

from winslow import ConfigOption, Workflow
from winslow.exceptions import MisconfigurationError


class Selecting(Workflow):
    """A workflow with a multiselect option. The form takes more than one
    value, and the command line must take them too."""

    regions = ConfigOption(
        choices=["eu", "us", "apac"],
        multiselect=True,
        default=["eu", "us"],
    )


def parse(*argv):
    return Selecting.get_parser().parse_args(list(argv))


def test_multiselect_takes_many_values():
    assert parse("--regions", "eu", "us", "apac").regions == ["eu", "us", "apac"]


def test_multiselect_single_value_is_a_list():
    # The shape of the value must not depend on how many values were passed.
    assert parse("--regions", "apac").regions == ["apac"]


def test_multiselect_default_applies():
    assert parse().regions == ["eu", "us"]


def test_multiselect_enforces_choices():
    # argparse validates each value against the choices, not the whole list.
    with pytest.raises(SystemExit):
        parse("--regions", "eu", "xx")


class Identified(Workflow):
    """An identifier option with a default. identifier implies required, but
    the default supplies the value on the command line."""

    region = ConfigOption(choices=["eu", "us"], default="eu", identifier=True)


def test_identifier_with_default_parses_without_the_flag():
    assert Identified.get_parser().parse_args([]).region == "eu"


def test_identifier_still_marks_the_option_required():
    # The form validator reads this flag, so a cleared field still errors.
    assert Identified.config_meta["region"].required is True


def test_format_value_joins_a_list():
    # A list value renders readable without a serializer (identifier labels).
    option = Selecting.config_meta["regions"]
    assert option.format_value(["eu", "us"]) == "eu, us"
    assert option.format_value("eu") == "eu"


class Demanding(Workflow):
    """A required option without a default stays mandatory on the command
    line."""

    region = ConfigOption(choices=["eu", "us"], required=True)


def test_required_without_default_is_demanded():
    with pytest.raises(SystemExit):
        Demanding.get_parser().parse_args([])


def test_cache_namespace_option_is_reserved():
    """cache_namespace is the framework property behind the storage identity
    (see Workflow.cache_namespace), so a project option cannot bind the name."""
    with pytest.raises(MisconfigurationError, match="cache_namespace.*clashes"):

        class Reserved(Workflow):
            cache_namespace = ConfigOption(default="x")
