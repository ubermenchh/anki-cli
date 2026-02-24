from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import pytest
from click.testing import CliRunner

import anki_cli.cli.commands.browse as browse_cmd_mod
from anki_cli.backends.factory import BackendFactoryError
from anki_cli.cli.commands.browse import cards_cmd
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


def _patch_session(monkeypatch, backend: Any) -> None:
    @contextmanager
    def fake_session(obj: dict[str, Any]):
        yield backend

    monkeypatch.setattr(browse_cmd_mod, "backend_session_from_context", fake_session)


def test_cards_command_is_registered() -> None:
    assert get_command("cards") is not None


def test_cards_cmd_success_launches_browse_app(monkeypatch: pytest.MonkeyPatch) -> None:
    run_called = {"count": 0}

    class FakeApp:
        def __init__(self, *, backend: Any, query: str = "") -> None:
            self.backend = backend
            self.query = query

        def run(self) -> None:
            run_called["count"] += 1

    class Backend:
        pass

    _patch_session(monkeypatch, Backend())
    monkeypatch.setattr(browse_cmd_mod, "BrowseApp", FakeApp, raising=False)

    # We need to patch the deferred import inside cards_cmd
    import anki_cli.tui.browse_app as browse_app_mod
    original_class = browse_app_mod.BrowseApp
    monkeypatch.setattr(browse_app_mod, "BrowseApp", FakeApp)

    runner = CliRunner()
    result = runner.invoke(cards_cmd, ["--query", "deck:Test"], obj=_base_obj())
    assert result.exit_code == 0
    assert run_called["count"] == 1


def test_cards_cmd_import_error_emits_tui_not_available(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if "browse_app" in name:
            raise ImportError("No module named 'textual'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    runner = CliRunner()
    result = runner.invoke(cards_cmd, ["--query", ""], obj=_base_obj())
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "TUI_NOT_AVAILABLE"
    assert "hint" in payload["error"]["details"]


def test_cards_cmd_backend_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_session(obj: dict[str, Any]):
        raise BackendFactoryError("backend down")

    monkeypatch.setattr(browse_cmd_mod, "backend_session_from_context", failing_session)

    runner = CliRunner()
    result = runner.invoke(cards_cmd, ["--query", ""], obj=_base_obj(backend="direct"))
    assert result.exit_code == 7
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "BACKEND_UNAVAILABLE"
