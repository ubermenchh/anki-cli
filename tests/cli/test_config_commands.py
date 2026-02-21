from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import anki_cli.cli.commands.config as config_cmd_mod
from anki_cli.cli.commands.config import config_cmd, config_path_cmd, config_set_cmd
from anki_cli.cli.dispatcher import get_command
from anki_cli.config_runtime import ConfigError, LoadedConfig
from anki_cli.models.config import AppConfig


def _base_obj(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "format": "json",
        "backend": "direct",
        "collection_path": None,
        "collection_override": None,
        "config_path": None,
        "requested_backend": "auto",
        "no_color": True,
        "copy": False,
        "app_config": None,
    }
    base.update(overrides)
    return base


def _invoke_success_json(
    command, 
    *, 
    args: list[str] | None = None, 
    obj: dict[str, Any]
) -> dict[str, Any]:
    runner = CliRunner()
    result = runner.invoke(command, args or [], obj=obj)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    return payload


def _invoke_error_json(
    command, 
    *, 
    args: list[str], 
    obj: dict[str, Any]
) -> tuple[Any, dict[str, Any]]:
    runner = CliRunner()
    result = runner.invoke(command, args, obj=obj)

    assert result.exit_code != 0
    stderr_text = getattr(result, "stderr", "")
    raw = stderr_text or result.output
    payload = json.loads(raw)
    assert payload["ok"] is False
    return result, payload


def test_config_cmd_emits_effective_and_app_config() -> None:
    app = AppConfig()
    app.backend.prefer = "direct"
    app.display.default_output = "csv"

    obj = _base_obj(
        app_config=app,
        config_path=Path("/tmp/config.toml"),
        requested_backend="ankiconnect",
        format="json",
        no_color=True,
        collection_override=Path("/tmp/override.db"),
    )

    payload = _invoke_success_json(config_cmd, obj=obj)

    assert payload["meta"]["command"] == "config"
    data = payload["data"]

    assert data["config_path"] == "/tmp/config.toml"
    assert data["effective"] == {
        "backend": "ankiconnect",
        "output_format": "json",
        "color": False,
        "collection_override": "/tmp/override.db",
    }
    assert data["config"]["backend"]["prefer"] == "direct"
    assert data["config"]["display"]["default_output"] == "csv"


def test_config_cmd_without_app_config_returns_empty_config() -> None:
    payload = _invoke_success_json(
        config_cmd,
        obj=_base_obj(app_config=None, requested_backend="auto", format="json", no_color=False),
    )

    data = payload["data"]
    assert data["config"] == {}
    assert data["effective"]["backend"] == "auto"
    assert data["effective"]["output_format"] == "json"
    assert data["effective"]["color"] is True
    assert data["effective"]["collection_override"] is None

def test_config_cmd_table_mode_smoke() -> None:
    runner = CliRunner()
    result = runner.invoke(config_cmd, [], obj=_base_obj(app_config=None, format="table"))
    assert result.exit_code == 0
    assert "effective" in result.output.lower()


def test_config_path_cmd_uses_context_paths() -> None:
    obj = _base_obj(
        collection_path=Path("/tmp/collection.db"),
        config_path=Path("/tmp/config.toml"),
    )

    payload = _invoke_success_json(config_path_cmd, obj=obj)
    data = payload["data"]

    assert payload["meta"]["command"] == "config:path"
    assert data["collection"] == "/tmp/collection.db"
    assert data["config"] == "/tmp/config.toml"
    assert data["backups"] == str(Path("~/.local/share/anki-cli/backups").expanduser())
    assert data["standalone_default"] == str(
        Path("~/.local/share/anki-cli/collection.db").expanduser()
    )
    assert data["anki_profiles"] == str(Path("~/.local/share/Anki2").expanduser())


def test_config_path_cmd_defaults_when_paths_absent() -> None:
    payload = _invoke_success_json(
        config_path_cmd, 
        obj=_base_obj(collection_path=None, config_path=None)
    )
    data = payload["data"]

    assert data["collection"] == "(auto)"
    assert data["config"] == str(Path("~/.config/anki-cli/config.toml").expanduser())


def test_config_set_cmd_success(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_set_config_value(*, key: str, raw_value: str, config_path: Path | None):
        captured["key"] = key
        captured["raw_value"] = raw_value
        captured["config_path"] = config_path
        loaded = LoadedConfig(app=AppConfig(), config_path=Path("/tmp/written.toml"), file_data={})
        return loaded, True, False

    monkeypatch.setattr(config_cmd_mod, "set_config_value", fake_set_config_value)

    payload = _invoke_success_json(
        config_set_cmd,
        args=["--key", "display.color", "--value", "false"],
        obj=_base_obj(config_path=Path("/tmp/source.toml")),
    )

    assert payload["meta"]["command"] == "config:set"
    assert payload["data"] == {
        "config_path": "/tmp/written.toml",
        "key": "display.color",
        "old_value": True,
        "new_value": False,
    }
    assert captured == {
        "key": "display.color",
        "raw_value": "false",
        "config_path": Path("/tmp/source.toml"),
    }


def test_config_set_cmd_maps_config_error_to_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_set_config_value(*, key: str, raw_value: str, config_path: Path | None):
        raise ConfigError("invalid value")

    monkeypatch.setattr(config_cmd_mod, "set_config_value", fake_set_config_value)

    result, payload = _invoke_error_json(
        config_set_cmd,
        args=["--key", "display.color", "--value", "nope"],
        obj=_base_obj(config_path=Path("/tmp/config.toml")),
    )

    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_CONFIG"
    assert "invalid value" in payload["error"]["message"]
    assert payload["error"]["details"] == {"key": "display.color", "value": "nope"}
    assert payload["meta"]["command"] == "config:set"


def test_config_commands_are_registered() -> None:
    assert get_command("config") is not None
    assert get_command("config:path") is not None
    assert get_command("config:set") is not None