from __future__ import annotations

import builtins
import sys
import types
from typing import Any

from click.testing import CliRunner

from anki_cli.cli.commands.shell import shell_cmd
from anki_cli.cli.dispatcher import get_command
from tests.cli.conftest import base_obj as _base_obj
from tests.cli.conftest import error_payload as _error_payload


def test_shell_cmd_success_calls_run_repl(monkeypatch) -> None:
    calls: dict[str, Any] = {}

    module = types.ModuleType("anki_cli.tui.repl")

    def fake_run_repl(obj: dict[str, Any]) -> None:
        calls["obj"] = obj

    module.run_repl = fake_run_repl  # type: ignore[assignment]
    monkeypatch.setitem(sys.modules, "anki_cli.tui.repl", module)

    runner = CliRunner()
    obj = _base_obj(backend="direct")
    result = runner.invoke(shell_cmd, [], obj=obj)

    assert result.exit_code == 0, result.output
    assert calls["obj"]["backend"] == "direct"


def test_shell_cmd_import_error_emits_tui_not_available_exit_2(monkeypatch) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, globals=None, locals=None, fromlist=(), level=0):
        if name == "anki_cli.tui.repl":
            raise ImportError("prompt_toolkit missing")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    runner = CliRunner()
    result = runner.invoke(shell_cmd, [], obj=_base_obj())

    payload = _error_payload(result)
    assert result.exit_code == 2
    assert payload["error"]["code"] == "TUI_NOT_AVAILABLE"
    assert "prompt_toolkit is not installed" in payload["error"]["message"]
    assert payload["error"]["details"] == {"hint": "Run: uv sync --extra tui"}
    assert payload["meta"]["command"] == "shell"


def test_shell_command_registered() -> None:
    assert get_command("shell") is not None
