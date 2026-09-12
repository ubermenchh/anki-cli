from __future__ import annotations

import json
import platform
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from anki_cli import __version__
from anki_cli.cli.commands.general import status_cmd, version_cmd
from anki_cli.cli.dispatcher import get_command


def _base_obj(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "format": "json",
        "backend": "direct",
        "collection_path": None,
        "no_color": True,
        "copy": False,
    }
    base.update(overrides)
    return base


def _invoke_json(command, *, obj: dict[str, Any]) -> dict[str, Any]:
    runner = CliRunner()
    result = runner.invoke(command, [], obj=obj)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    return payload


def test_version_cmd_emits_expected_json_payload() -> None:
    col = Path("/tmp/collection.db")
    payload = _invoke_json(version_cmd, obj=_base_obj(backend="direct", collection_path=col))

    assert payload["meta"]["command"] == "version"
    assert payload["meta"]["backend"] == "direct"
    assert payload["meta"]["collection"] == str(col)

    data = payload["data"]
    assert data["version"] == __version__
    assert data["python"] == platform.python_version()
    assert data["backend"] == "direct"
    assert data["collection"] == str(col)


def test_status_cmd_emits_backend_and_collection() -> None:
    payload = _invoke_json(status_cmd, obj=_base_obj(backend="ankiconnect", collection_path=None))

    assert payload["meta"]["command"] == "status"

    data = payload["data"]
    assert data == {
        "backend": "ankiconnect",
        "collection": None,
    }


def test_general_commands_are_registered() -> None:
    assert get_command("version") is not None
    assert get_command("status") is not None
