from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest
from click.testing import CliRunner

import anki_cli.backends.factory as factory_mod
from anki_cli.backends.factory import BackendFactoryError
from anki_cli.cli.commands.browse import browse_cmd
from anki_cli.cli.dispatcher import get_command
from tests.cli.conftest import base_obj as _base_obj
from tests.cli.conftest import patch_session as _patch_session


def _patch_browse_module(monkeypatch: pytest.MonkeyPatch, app_cls: type[Any]) -> None:
    module = types.ModuleType("anki_cli.tui.browse_app")
    module.BrowseApp = app_cls
    monkeypatch.setitem(sys.modules, "anki_cli.tui.browse_app", module)


def test_browse_command_is_registered() -> None:
    assert get_command("browse") is not None


def test_browse_cmd_success_launches_browse_app(monkeypatch: pytest.MonkeyPatch) -> None:
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
    _patch_browse_module(monkeypatch, FakeApp)

    runner = CliRunner()
    result = runner.invoke(browse_cmd, ["--query", "deck:Test"], obj=_base_obj())
    assert result.exit_code == 0
    assert run_called["count"] == 1


def test_browse_cmd_import_error_emits_tui_not_available(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    monkeypatch.delitem(sys.modules, "anki_cli.tui.browse_app", raising=False)

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "anki_cli.tui.browse_app":
            raise ImportError("No module named 'textual'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    runner = CliRunner()
    result = runner.invoke(browse_cmd, ["--query", ""], obj=_base_obj())
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "TUI_NOT_AVAILABLE"
    assert "hint" in payload["error"]["details"]


def test_browse_cmd_backend_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeApp:
        def __init__(self, *, backend: Any, query: str = "") -> None:
            pass

        def run(self) -> None:
            pass

    _patch_browse_module(monkeypatch, FakeApp)

    def failing_session(obj: dict[str, Any]):
        raise BackendFactoryError("backend down")

    monkeypatch.setattr(factory_mod, "backend_session_from_context", failing_session)

    runner = CliRunner()
    result = runner.invoke(browse_cmd, ["--query", ""], obj=_base_obj(backend="direct"))
    assert result.exit_code == 7
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "BACKEND_UNAVAILABLE"


def test_browse_cmd_tui_crash_emits_internal_error_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#83: Textual swallows handler exceptions, prints a traceback and sets
    ``return_code = 1`` — ``run()`` returns normally. The CLI must still emit a
    failure envelope and a non-zero exit instead of exiting 0."""

    class CrashingApp:
        return_code = 1
        _exception = RuntimeError("collection is locked")

        def __init__(self, *, backend: Any, query: str = "") -> None:
            pass

        def run(self) -> None:
            return None

    class Backend:
        pass

    _patch_session(monkeypatch, Backend())
    _patch_browse_module(monkeypatch, CrashingApp)

    runner = CliRunner()
    result = runner.invoke(browse_cmd, ["--query", ""], obj=_base_obj())
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert payload["error"]["details"]["return_code"] == 1
    assert "collection is locked" in payload["error"]["details"]["exception"]


def test_browse_cmd_clean_exit_has_no_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """``q`` -> ``App.exit()`` -> ``return_code == 0``; nothing to report."""

    class CleanApp:
        return_code = 0

        def __init__(self, *, backend: Any, query: str = "") -> None:
            pass

        def run(self) -> None:
            return None

    class Backend:
        pass

    _patch_session(monkeypatch, Backend())
    _patch_browse_module(monkeypatch, CleanApp)

    result = CliRunner().invoke(browse_cmd, ["--query", ""], obj=_base_obj())
    assert result.exit_code == 0
    assert result.output == ""
