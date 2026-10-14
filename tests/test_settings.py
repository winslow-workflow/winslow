"""The settings object: the base of winslow.conf.defaults under the project
module WINSLOW_SETTINGS_MODULE names, loaded on the first read."""

import os
import subprocess
import sys
import types

import pytest

from winslow.conf import Settings, import_string, load, settings
from winslow.conf import defaults
from winslow.conf.report import lines
from winslow.exceptions import MisconfigurationError
from winslow.orchestrator import Command, Orchestrator, OrchestratorConfig


def module(name, **names):
    """A project settings module under `name`, importable for the test."""
    created = types.ModuleType(name)
    vars(created).update(names)
    sys.modules[name] = created
    return name


@pytest.fixture
def forget():
    names = []
    yield names
    for name in names:
        sys.modules.pop(name, None)


def test_settings_py_in_the_working_directory_is_the_module_unless_named(
    tmp_path, monkeypatch, forget
):
    from winslow.conf.load import module_name

    monkeypatch.delenv("WINSLOW_SETTINGS_MODULE", raising=False)
    monkeypatch.chdir(tmp_path)
    assert module_name() is None
    (tmp_path / "settings.py").write_text('ENV = "lab"\n')
    forget.append("settings")
    assert module_name() == "settings"
    assert load(module_name()).names["ENV"] == "lab"
    monkeypatch.setenv("WINSLOW_SETTINGS_MODULE", "winslow.conf.template")
    assert module_name() == "winslow.conf.template"


def test_a_broken_settings_py_in_the_working_directory_names_the_file(
    tmp_path, monkeypatch, forget
):
    from winslow.conf.load import module_name

    monkeypatch.delenv("WINSLOW_SETTINGS_MODULE", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "settings.py").write_text('raise ValueError("django")\n')
    # An earlier test left its own directory on sys.path: this one goes first.
    monkeypatch.syspath_prepend(str(tmp_path))
    forget.append("settings")
    with pytest.raises(
        MisconfigurationError, match=f"settings.py in {tmp_path} does not import"
    ):
        load(module_name())


def test_the_base_alone_loads_without_a_module():
    loaded = load(None)
    assert loaded.module is None
    assert loaded.names["ENV"] == "local"
    assert loaded.source("ENV") == "default"
    assert set(loaded.names) >= {"ENV", "ENV_COLORS", "STATE_STORE", "TELEMETRY"}


def test_a_module_overrides_the_base_and_adds_its_own_names(forget):
    forget.append(module("etl_settings", ENV="qa", OKTA_ISSUER="https://x", lower="no"))
    loaded = load("etl_settings")
    assert loaded.names["ENV"] == "qa"
    assert loaded.source("ENV") == "module"
    assert loaded.names["OKTA_ISSUER"] == "https://x"
    assert "lower" not in loaded.names
    assert loaded.names["ENV_COLORS"] == defaults.ENV_COLORS
    # Free form: a value of another type than the default is kept as given.
    assert load(module("typed_settings", LOG_UTC="false")).names["LOG_UTC"] == "false"
    forget.append("typed_settings")


def test_a_module_that_does_not_import_names_the_variable(monkeypatch):
    monkeypatch.setenv("WINSLOW_SETTINGS_MODULE", "no.such")
    with pytest.raises(
        MisconfigurationError, match="WINSLOW_SETTINGS_MODULE='no.such' does not import"
    ):
        load("no.such")


def test_import_string_resolves_a_path_and_names_a_bad_one():
    assert import_string("winslow.state.FileStateStore").__name__ == "FileStateStore"
    with pytest.raises(MisconfigurationError, match="'nodots' is not a dotted path"):
        import_string("nodots")
    with pytest.raises(MisconfigurationError, match="winslow.state has no 'Nope'"):
        import_string("winslow.state.Nope")
    with pytest.raises(MisconfigurationError, match="does not import"):
        import_string("no.such.Thing")


def test_the_object_loads_on_the_first_read_and_keeps_plain_attributes(
    monkeypatch, forget
):
    forget.append(module("obj_settings", ENV="uat"))
    monkeypatch.setenv("WINSLOW_SETTINGS_MODULE", "obj_settings")
    fresh = Settings()
    assert "ENV" not in vars(fresh)
    assert fresh.ENV == "uat"
    assert vars(fresh)["ENV"] == "uat"
    assert fresh.loaded.source("ENV") == "module"
    with pytest.raises(AttributeError, match="no 'NOPE'"):
        fresh.NOPE


def test_a_path_setting_resolves_at_its_call_site(monkeypatch):
    from winslow.access.backends import backend_classes
    from winslow.state import FileStateStore

    assert import_string(settings.STATE_STORE) is FileStateStore
    assert backend_classes()[0].name == "token"
    monkeypatch.setattr(settings, "STATE_STORE", "winslow.state.Nope")
    with pytest.raises(MisconfigurationError, match="winslow.state has no 'Nope'"):
        import_string(settings.STATE_STORE)


def test_env_color_follows_env_and_a_module_table_replaces_the_defaults(monkeypatch):
    monkeypatch.setattr(settings, "ENV", "uat")
    assert settings.env_color() == "gold"
    monkeypatch.setattr(settings, "ENV_COLORS", {"uat": "purple"})
    assert settings.env_color() == "purple"
    monkeypatch.setattr(settings, "ENV", "qa")
    assert settings.env_color() == "gray"


def test_importing_winslow_leaves_the_settings_unloaded():
    probe = (
        "import winslow, winslow.orchestrator, winslow.client.local\n"
        "from winslow.conf import settings\n"
        "print(settings._loaded is None)"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "True"


def test_the_template_imports_and_holds_every_base_name():
    from winslow.conf import template

    assert {k for k in vars(template) if k.isupper()} >= {
        k for k in vars(defaults) if k.isupper()
    }


def test_a_read_during_the_project_module_load_names_the_module(tmp_path):
    (tmp_path / "reading_settings.py").write_text(
        "from winslow.conf import settings\nSEEN = settings.ENV\n"
    )
    probe = "from winslow.conf import settings\nsettings.ENV\n"
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "WINSLOW_SETTINGS_MODULE": "reading_settings",
            "PYTHONPATH": os.pathsep.join([str(tmp_path), *sys.path]),
        },
    )
    assert out.returncode != 0
    assert "reading_settings reads a setting while the settings load" in out.stderr


def test_a_module_that_raises_at_import_names_the_variable_and_the_error(
    monkeypatch, tmp_path, forget
):
    (tmp_path / "raising_settings.py").write_text('raise ValueError("boom")\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    forget.append("raising_settings")
    with pytest.raises(
        MisconfigurationError, match="does not import - ValueError: boom"
    ):
        load("raising_settings")


def test_import_string_refuses_a_relative_path_and_a_trailing_dot():
    with pytest.raises(MisconfigurationError, match="'.ui.Pane' is not a dotted path"):
        import_string(".ui.Pane")
    with pytest.raises(MisconfigurationError, match="'winslow.' is not a dotted path"):
        import_string("winslow.")


def test_the_report_names_the_source_masks_the_secrets_and_continues_past_a_bad_path(
    monkeypatch,
):
    monkeypatch.setattr(settings, "AUTH_TICKET_SECRET", "x" * 40)
    monkeypatch.setattr(
        settings, "TUI_PLUGINS", ["nope.Missing", "winslow.ui.plugin.UIPlugin"]
    )
    report = list(lines(settings))
    assert report[0] == "settings module: none"
    assert "AUTH_TICKET_SECRET = set  [default]" in report
    assert "SENTRY_DSN = unset  [default]" in report
    assert "STATE_STORE -> winslow.state.FileStateStore" in report
    assert "TELEMETRY -> nothing" in report
    assert (
        "TUI_PLUGINS: nope.Missing -> 'nope.Missing' does not import - No module named 'nope'"
        in report
    )
    assert "TUI_PLUGINS -> winslow.ui.plugin.UIPlugin" in report


def test_the_template_passes_the_report(monkeypatch):
    monkeypatch.setenv("WINSLOW_SETTINGS_MODULE", "winslow.conf.template")
    report = list(lines(Settings()))
    assert report[0] == "settings module: winslow.conf.template"
    assert "AUTH_BACKENDS -> winslow.access.backends.TokenBackend" in report


def test_the_settings_command_runs_without_workflows(tmp_path, capsys):
    config, unknown = Orchestrator.get_base_parser().parse_known_args(
        ["settings"], namespace=OrchestratorConfig()
    )
    assert config.action is Command.SETTINGS and unknown == []
    assert Orchestrator(config, directory=tmp_path).start() is True
    assert capsys.readouterr().out.startswith("settings module: none")


def test_a_listed_configuration_whose_module_does_not_import_names_the_setting(
    monkeypatch,
):
    monkeypatch.setitem(sys.modules, "winslow.contrib.fake", None)
    monkeypatch.setattr(settings, "TELEMETRY", ["winslow.contrib.fake.Fake"])
    with pytest.raises(
        MisconfigurationError, match="'winslow.contrib.fake.Fake' does not import"
    ):
        import_string(settings.TELEMETRY[0])


def test_the_auth_commands_import_without_the_serve_extra():
    """The connect extra carries pyjwt, so the probe blocks the packages of
    the serve extra alone."""
    probe = (
        "import sys\n"
        "sys.modules['starlette'] = sys.modules['uvicorn'] = sys.modules['mcp'] = None\n"
        "import winslow.access.cli, winslow.access.backends, winslow.orchestrator\n"
        "print('ok')"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "ok"
