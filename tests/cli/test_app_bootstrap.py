from __future__ import annotations

import builtins
import json
import sys
import types
from pathlib import Path
from typing import Any

import click
from click.testing import CliRunner

import anki_cli.cli.app as app_mod
from anki_cli import __version__
from anki_cli.backends.detect import DetectionError, DetectionResult
from anki_cli.config_runtime import ConfigError
from anki_cli.models.config import AppConfig


def _runtime(
    *,
    backend: str = "direct",
    output_format: str = "json",
    no_color: bool = True,
    collection_override: Path | None = None,
):
    return types.SimpleNamespace(
        app=AppConfig(),
        config_path=Path("/tmp/config.toml"),
        backend=backend,
        output_format=output_format,
        no_color=no_color,
        collection_override=collection_override,
    )


def _error_payload(result) -> dict[str, Any]:
    assert result.exit_code != 0
    raw = (getattr(result, "stderr", "") or result.output).strip()
    payload = json.loads(raw)
    assert payload["ok"] is False
    return payload


def _install_dummy_command(monkeypatch):
    captured: dict[str, Any] = {}

    @click.command("dummy")
    @click.pass_context
    def dummy_cmd(ctx: click.Context) -> None:
        captured["obj"] = dict(ctx.obj or {})
        click.echo("dummy-ran")

    monkeypatch.setattr(app_mod, "list_commands", lambda: ["dummy"])
    monkeypatch.setattr(
        app_mod,
        "get_command",
        lambda name: dummy_cmd if name == "dummy" else None,
    )
    return captured


def test_version_flag_exits_early_without_bootstrap_calls(monkeypatch) -> None:
    def fail_resolve(**kwargs: Any):
        raise AssertionError("resolve_runtime_config should not run on --version")

    def fail_detect(**kwargs: Any):
        raise AssertionError("detect_backend should not run on --version")

    monkeypatch.setattr(app_mod, "resolve_runtime_config", fail_resolve)
    monkeypatch.setattr(app_mod, "detect_backend", fail_detect)

    runner = CliRunner()
    result = runner.invoke(app_mod.main, ["--version"])

    assert result.exit_code == 0
    assert result.output.strip() == f"anki-cli {__version__}"


def test_config_error_emits_invalid_config_exit_2(monkeypatch) -> None:
    def fail_resolve(**kwargs: Any):
        raise ConfigError("broken config")

    monkeypatch.setattr(app_mod, "resolve_runtime_config", fail_resolve)

    runner = CliRunner()
    result = runner.invoke(app_mod.main, ["--format", "json"])

    payload = _error_payload(result)
    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_CONFIG"
    assert "broken config" in payload["error"]["message"]
    assert payload["meta"]["command"] == "bootstrap"


def test_detection_error_emits_backend_unavailable_with_exit_code(monkeypatch) -> None:
    runtime = _runtime(backend="standalone", output_format="json")
    monkeypatch.setattr(app_mod, "resolve_runtime_config", lambda **kwargs: runtime)

    def fail_detect(**kwargs: Any):
        raise DetectionError("backend unavailable", exit_code=9)

    monkeypatch.setattr(app_mod, "detect_backend", fail_detect)

    runner = CliRunner()
    result = runner.invoke(app_mod.main, ["--format", "json"])

    payload = _error_payload(result)
    assert result.exit_code == 9
    assert payload["error"]["code"] == "BACKEND_UNAVAILABLE"
    assert payload["error"]["details"] == {"forced_backend": "standalone"}
    assert payload["meta"]["command"] == "bootstrap"


def test_bootstrap_success_passes_context_to_subcommand(monkeypatch) -> None:
    captured = _install_dummy_command(monkeypatch)

    runtime = _runtime(
        backend="direct",
        output_format="json",
        no_color=False,
        collection_override=Path("/tmp/override.db"),
    )
    monkeypatch.setattr(app_mod, "resolve_runtime_config", lambda **kwargs: runtime)

    detection = DetectionResult(
        backend="direct",
        collection_path=Path("/tmp/detected.db"),
        reason="forced",
    )
    monkeypatch.setattr(app_mod, "detect_backend", lambda **kwargs: detection)

    runner = CliRunner()
    result = runner.invoke(app_mod.main, ["dummy"])

    assert result.exit_code == 0, result.output
    assert "dummy-ran" in result.output

    obj = captured["obj"]
    assert obj["format"] == "json"
    assert obj["no_color"] is False
    assert obj["requested_backend"] == "direct"
    assert obj["config_path"] == Path("/tmp/config.toml")
    assert isinstance(obj["app_config"], AppConfig)
    assert obj["collection_override"] == Path("/tmp/override.db")
    assert obj["collection_path"] == Path("/tmp/detected.db")
    assert obj["backend"] == "direct"
    assert obj["backend_reason"] == "forced"


def test_cli_parameter_sources_marked_when_explicit(monkeypatch) -> None:
    _install_dummy_command(monkeypatch)
    captured_kwargs: dict[str, Any] = {}

    def fake_resolve(**kwargs: Any):
        captured_kwargs.update(kwargs)
        return _runtime(
            backend="direct",
            output_format="json",
            no_color=True,
            collection_override=Path("/tmp/override.db"),
        )

    monkeypatch.setattr(app_mod, "resolve_runtime_config", fake_resolve)
    monkeypatch.setattr(
        app_mod,
        "detect_backend",
        lambda **kwargs: DetectionResult(
            backend="direct",
            collection_path=Path("/tmp/detected.db"),
            reason="ok",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(
        app_mod.main,
        [
            "--backend",
            "direct",
            "--format",
            "json",
            "--no-color",
            "--col",
            "/tmp/cli-col.db",
            "dummy",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured_kwargs["cli_backend"] == "direct"
    assert captured_kwargs["cli_backend_set"] is True
    assert captured_kwargs["cli_output_format"] == "json"
    assert captured_kwargs["cli_output_set"] is True
    assert captured_kwargs["cli_no_color"] is True
    assert captured_kwargs["cli_no_color_set"] is True
    assert captured_kwargs["cli_collection_path"] == Path("/tmp/cli-col.db")
    assert captured_kwargs["cli_collection_set"] is True


def test_cli_parameter_sources_not_marked_when_defaults(monkeypatch) -> None:
    _install_dummy_command(monkeypatch)
    captured_kwargs: dict[str, Any] = {}

    def fake_resolve(**kwargs: Any):
        captured_kwargs.update(kwargs)
        return _runtime(
            backend="auto",
            output_format="table",
            no_color=False,
            collection_override=None,
        )

    monkeypatch.setattr(app_mod, "resolve_runtime_config", fake_resolve)
    monkeypatch.setattr(
        app_mod,
        "detect_backend",
        lambda **kwargs: DetectionResult(
            backend="standalone",
            collection_path=Path("/tmp/standalone.db"),
            reason="fallback",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(app_mod.main, ["dummy"])

    assert result.exit_code == 0, result.output
    assert captured_kwargs["cli_backend"] == "auto"
    assert captured_kwargs["cli_backend_set"] is False
    assert captured_kwargs["cli_output_format"] == "table"
    assert captured_kwargs["cli_output_set"] is False
    assert captured_kwargs["cli_no_color"] is False
    assert captured_kwargs["cli_no_color_set"] is False
    assert captured_kwargs["cli_collection_path"] is None
    assert captured_kwargs["cli_collection_set"] is False


def test_no_subcommand_runs_repl_when_available(monkeypatch) -> None:
    calls: dict[str, Any] = {}

    module = types.ModuleType("anki_cli.tui.repl")

    def fake_run_repl(obj: dict[str, Any]) -> None:
        calls["obj"] = dict(obj)

    module.run_repl = fake_run_repl  # type: ignore[assignment]
    monkeypatch.setitem(sys.modules, "anki_cli.tui.repl", module)

    monkeypatch.setattr(
        app_mod,
        "resolve_runtime_config",
        lambda **kwargs: _runtime(
            backend="direct",
            output_format="json",
            no_color=True,
            collection_override=Path("/tmp/override.db"),
        ),
    )
    monkeypatch.setattr(
        app_mod,
        "detect_backend",
        lambda **kwargs: DetectionResult(
            backend="direct",
            collection_path=Path("/tmp/detected.db"),
            reason="ok",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(app_mod.main, [])

    assert result.exit_code == 0, result.output
    assert calls["obj"]["backend"] == "direct"
    assert calls["obj"]["collection_path"] == Path("/tmp/detected.db")
    assert calls["obj"]["backend_reason"] == "ok"


def test_no_subcommand_import_error_falls_back_to_help(monkeypatch) -> None:
    monkeypatch.setattr(
        app_mod,
        "resolve_runtime_config",
        lambda **kwargs: _runtime(
            backend="direct",
            output_format="json",
            no_color=True,
            collection_override=None,
        ),
    )
    monkeypatch.setattr(
        app_mod,
        "detect_backend",
        lambda **kwargs: DetectionResult(
            backend="direct",
            collection_path=Path("/tmp/detected.db"),
            reason="ok",
        ),
    )

    monkeypatch.setattr(app_mod, "list_commands", lambda: [])
    monkeypatch.setattr(app_mod, "get_command", lambda name: None)

    real_import = builtins.__import__

    def fake_import(name: str, globals=None, locals=None, fromlist=(), level=0):
        if name == "anki_cli.tui.repl":
            raise ImportError("prompt_toolkit missing")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    runner = CliRunner()
    result = runner.invoke(app_mod.main, [])

    assert result.exit_code == 0
    assert "Usage:" in result.output
    assert "--backend" in result.output