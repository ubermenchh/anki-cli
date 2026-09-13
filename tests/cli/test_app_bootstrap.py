from __future__ import annotations

import builtins
import json
import sys
import types
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

import anki_cli.backends.factory as factory_mod
import anki_cli.cli.app as app_mod
import anki_cli.cli.commands.general as general_mod
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
        warnings=[],
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
    _install_dummy_command(monkeypatch)
    runtime = _runtime(backend="direct", output_format="json")
    monkeypatch.setattr(app_mod, "resolve_runtime_config", lambda **kwargs: runtime)

    def fail_detect(**kwargs: Any):
        raise DetectionError("backend unavailable", exit_code=9)

    monkeypatch.setattr(app_mod, "detect_backend", fail_detect)

    runner = CliRunner()
    result = runner.invoke(app_mod.main, ["--format", "json", "dummy"])

    payload = _error_payload(result)
    assert result.exit_code == 9
    assert payload["error"]["code"] == "BACKEND_UNAVAILABLE"
    assert payload["error"]["details"] == {"forced_backend": "direct"}
    assert payload["meta"]["command"] == "bootstrap"


def _raise_exit3(**kwargs: Any):
    raise DetectionError("no AnkiConnect and no collection found", exit_code=3)


@pytest.mark.parametrize(
    "argv",
    [
        ["version"],
        ["status"],
        ["config"],
        ["config:path"],
        ["config:set", "--key", "display.color", "--value", "false"],
    ],
)
def test_backend_free_commands_skip_detection(
    monkeypatch, tmp_path: Path, argv
) -> None:
    """The commands a locked-out user needs must not pay for — or die on —
    backend detection."""
    runtime = _runtime(
        backend="auto",
        output_format="json",
        collection_override=tmp_path / "override.anki2",
    )
    runtime.config_path = tmp_path / "config.toml"  # keep config:set off the real disk
    monkeypatch.setattr(app_mod, "resolve_runtime_config", lambda **kwargs: runtime)
    monkeypatch.setattr(app_mod, "detect_backend", _raise_exit3)
    # `status` re-probes via its own module reference; make it fail there too.
    monkeypatch.setattr(general_mod, "detect_backend", _raise_exit3)

    result = CliRunner().invoke(app_mod.main, ["--format", "json", *argv])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True


def test_status_reports_detection_failure_as_data(monkeypatch) -> None:
    """`status` turns DetectionError into exit-0 data, not an exit code."""
    runtime = _runtime(backend="auto", output_format="json")
    monkeypatch.setattr(app_mod, "resolve_runtime_config", lambda **kwargs: runtime)
    monkeypatch.setattr(app_mod, "detect_backend", _raise_exit3)
    monkeypatch.setattr(general_mod, "detect_backend", _raise_exit3)

    result = CliRunner().invoke(app_mod.main, ["--format", "json", "status"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["data"]["ok"] is False
    assert payload["data"]["backend"] is None
    assert "no AnkiConnect" in payload["data"]["error"]


def test_status_reports_detection_result(monkeypatch, tmp_path: Path) -> None:
    db = tmp_path / "Work" / "collection.anki2"
    db.parent.mkdir(parents=True)
    db.touch()
    detection = DetectionResult(
        backend="direct",
        collection_path=db,
        reason="forced",
        profile="Work",
    )
    runtime = _runtime(backend="direct", output_format="json")
    runtime.warnings = ["stale collection.path ignored"]
    monkeypatch.setattr(app_mod, "resolve_runtime_config", lambda **kwargs: runtime)
    monkeypatch.setattr(app_mod, "detect_backend", lambda **kwargs: detection)
    monkeypatch.setattr(general_mod, "detect_backend", lambda **kwargs: detection)

    result = CliRunner().invoke(app_mod.main, ["--format", "json", "status"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"] == {
        "ok": True,
        "backend": "direct",
        "collection": str(db),
        "reason": "forced",
        "profile": "Work",
    }
    # Bootstrap notices reach the consumer inside the envelope, not as a
    # bare non-JSON line on stderr.
    assert payload["meta"]["warnings"] == ["stale collection.path ignored"]


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


def test_bootstrap_forwards_anki_profile_to_detect(monkeypatch) -> None:
    # Pins the anki_profile= kwarg at the detect_backend call site.
    _install_dummy_command(monkeypatch)

    runtime = _runtime(backend="direct", output_format="json")
    runtime.app.collection.anki_profile = "Work"
    monkeypatch.setattr(app_mod, "resolve_runtime_config", lambda **kwargs: runtime)

    captured: dict[str, Any] = {}

    def fake_detect(**kwargs: Any):
        captured.update(kwargs)
        return DetectionResult(
            backend="direct",
            collection_path=Path("/tmp/detected.db"),
            reason="forced",
        )

    monkeypatch.setattr(app_mod, "detect_backend", fake_detect)

    result = CliRunner().invoke(app_mod.main, ["dummy"])

    assert result.exit_code == 0, result.output
    assert captured["anki_profile"] == "Work"
    assert captured["forced_backend"] == "direct"
    assert captured["col_override"] is None


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
            backend="direct",
            collection_path=Path("/tmp/detected.db"),
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


def test_no_subcommand_runs_repl_when_available(monkeypatch, tmp_path: Path) -> None:
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

    # The REPL runs many backend commands, so detection must happen up front
    # and the handed-over context must be constructible by the factory.
    db = tmp_path / "collection.anki2"
    db.touch()
    detection = DetectionResult(
        backend="direct",
        collection_path=db,
        reason="forced",
        profile=None,
    )
    monkeypatch.setattr(app_mod, "detect_backend", lambda **kwargs: detection)

    runner = CliRunner()
    result = runner.invoke(app_mod.main, [])

    assert result.exit_code == 0, result.output
    assert calls["obj"]["backend"] == "direct"
    assert calls["obj"]["collection_path"] == db
    assert calls["obj"]["backend_reason"] == "forced"

    # Constructibility pin: a "none" backend here left every REPL command dead.
    backend = factory_mod.create_backend_from_context(calls["obj"])
    assert backend.collection_path == db.resolve()


def test_no_subcommand_detection_failure_opens_repl_with_warning(
    monkeypatch,
) -> None:
    """Bare `anki` on a host with no Anki still opens the REPL; the warning
    explains why backend commands will fail."""
    calls: dict[str, Any] = {}

    module = types.ModuleType("anki_cli.tui.repl")

    def fake_run_repl(obj: dict[str, Any]) -> None:
        calls["obj"] = dict(obj)

    module.run_repl = fake_run_repl  # type: ignore[assignment]
    monkeypatch.setitem(sys.modules, "anki_cli.tui.repl", module)

    monkeypatch.setattr(
        app_mod,
        "resolve_runtime_config",
        lambda **kwargs: _runtime(backend="auto", output_format="json"),
    )
    monkeypatch.setattr(app_mod, "detect_backend", _raise_exit3)

    runner = CliRunner()
    result = runner.invoke(app_mod.main, [])

    assert result.exit_code == 0, result.output
    assert calls["obj"]["backend"] == "none"
    assert "no AnkiConnect" in calls["obj"]["backend_reason"]
    combined = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "warning:" in combined
    assert "backend commands unavailable" in combined


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
            reason="forced",
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


def test_option_values_containing_equals_reach_the_subcommand_intact(monkeypatch) -> None:
    """Regression for #25: `--query "prop:lapses=0"` used to be rewritten to
    `--query --prop:lapses 0` by the key=value sugar. key=value sugar itself
    must keep working, including an '=' inside the value."""
    seen: list[str] = []

    @click.command("probe")
    @click.option("--query", required=True)
    @click.option("--flag", is_flag=True)
    def probe_cmd(query: str, flag: bool) -> None:
        seen.append(query)
        click.echo("probe-ran")

    monkeypatch.setattr(app_mod, "list_commands", lambda: ["probe"])
    monkeypatch.setattr(app_mod, "get_command", lambda name: probe_cmd if name == "probe" else None)
    monkeypatch.setattr(app_mod, "resolve_runtime_config", lambda **kwargs: _runtime())
    monkeypatch.setattr(
        app_mod,
        "detect_backend",
        lambda **kwargs: DetectionResult(backend="direct", collection_path=None, reason="forced"),
    )
    runner = CliRunner()

    for argv in (
        ["probe", "--query", "prop:lapses=0"],
        ["probe", "query=prop:lapses=0"],
        ["probe", "--flag", "query=prop:lapses=0"],
        ["--format", "json", "probe", "--query", "prop:lapses=0"],
    ):
        result = runner.invoke(app_mod.main, argv)
        assert result.exit_code == 0, (argv, result.output)
        assert "probe-ran" in result.output

    assert seen == ["prop:lapses=0"] * 4
