from __future__ import annotations

import json
import platform
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import anki_cli.cli.commands.general as general_mod
from anki_cli import __version__
from anki_cli.backends.detect import DetectionError, DetectionResult
from anki_cli.cli.commands.general import status_cmd, version_cmd
from anki_cli.cli.dispatcher import get_command
from anki_cli.models.config import AppConfig


def _base_obj(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "format": "json",
        "backend": "direct",
        "collection_path": None,
        "requested_backend": "auto",
        "collection_override": None,
        "app_config": AppConfig(),
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


def test_status_cmd_reports_detection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "Work" / "collection.anki2"
    db.parent.mkdir(parents=True)
    db.touch()

    captured: dict[str, Any] = {}

    def fake_detect(**kwargs: Any) -> DetectionResult:
        captured.update(kwargs)
        return DetectionResult(
            backend="ankiconnect",
            collection_path=db,
            reason="ankiconnect reachable",
            profile="Work",
        )

    monkeypatch.setattr(general_mod, "detect_backend", fake_detect)

    payload = _invoke_json(status_cmd, obj=_base_obj(requested_backend="ankiconnect"))

    assert payload["meta"]["command"] == "status"
    assert captured["forced_backend"] == "ankiconnect"
    assert captured["anki_profile"] is None

    data = payload["data"]
    assert data == {
        "ok": True,
        "backend": "ankiconnect",
        "collection": str(db),
        "reason": "ankiconnect reachable",
        "profile": "Work",
    }


def test_status_cmd_detection_failure_is_data_not_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_detect(**kwargs: Any) -> DetectionResult:
        raise DetectionError("nothing found", exit_code=3)

    monkeypatch.setattr(general_mod, "detect_backend", fail_detect)

    payload = _invoke_json(status_cmd, obj=_base_obj())

    data = payload["data"]
    assert data["ok"] is False
    assert data["backend"] is None
    assert data["collection"] is None
    assert "nothing found" in data["error"]


def test_general_commands_are_registered() -> None:
    assert get_command("version") is not None
    assert get_command("status") is not None
