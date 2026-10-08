"""The settings module: the environment color table and its override."""

from winslow import settings


def test_pairs_parse_a_comma_list_and_skip_blanks():
    assert settings._pairs("qa=purple, staging = #ff8800,,") == {
        "qa": "purple",
        "staging": "#ff8800",
    }
    assert settings._pairs("") == {}


def test_the_defaults_name_the_five_environments():
    assert set(settings.ENV_COLORS) >= {"prod", "uat", "staging", "dev", "local"}
    assert settings.ENV_COLORS[settings.env] == settings.ENV_COLOR
