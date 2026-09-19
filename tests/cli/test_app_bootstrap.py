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


def _never_detect(**kwargs: Any):
    raise AssertionError("detect_backend must not run here")


def _install_dummy_command(monkeypatch, *, opens_session: bool = False):
    """A subcommand that records ``ctx.obj``; with ``opens_session`` it also
    opens a backend session, which is what triggers lazy detection (#32)."""
    captured: dict[str, Any] = {}

    @click.command("dummy")
    @click.pass_context
    def dummy_cmd(ctx: click.Context) -> None:
        if opens_session:
            with factory_mod.backend_session_from_context(ctx.obj) as backend:
                captured["backend"] = backend
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
    """Detection runs when the command first opens a session (#32); its exit
    code survives the trip through the factory, and the envelope names the
    command rather than "bootstrap"."""
    _install_dummy_command(monkeypatch, opens_session=True)
    runtime = _runtime(backend="direct", output_format="json")
    monkeypatch.setattr(app_mod, "resolve_runtime_config", lambda **kwargs: runtime)

    def fail_detect(**kwargs: Any):
        raise DetectionError("backend unavailable", exit_code=9)

    monkeypatch.setattr(app_mod, "detect_backend", _never_detect)
    monkeypatch.setattr(factory_mod, "detect_backend", fail_detect)

    runner = CliRunner()
    result = runner.invoke(app_mod.main, ["--format", "json", "dummy"])

    payload = _error_payload(result)
    assert result.exit_code == 9
    assert payload["error"]["code"] == "BACKEND_UNAVAILABLE"
    assert payload["error"]["message"] == "backend unavailable"
    assert payload["meta"]["command"] == "dummy"


def test_subcommand_that_never_opens_a_session_never_detects(monkeypatch) -> None:
    """The whole point of lazy detection: no HTTP probe, no pgrep, no lock
    probe unless a backend is actually needed."""
    captured = _install_dummy_command(monkeypatch)
    runtime = _runtime(backend="auto", output_format="json")
    monkeypatch.setattr(app_mod, "resolve_runtime_config", lambda **kwargs: runtime)
    monkeypatch.setattr(app_mod, "detect_backend", _never_detect)
    monkeypatch.setattr(factory_mod, "detect_backend", _never_detect)

    result = CliRunner().invoke(app_mod.main, ["dummy"])

    assert result.exit_code == 0, result.output
    assert captured["obj"]["backend"] == "auto"
    assert captured["obj"]["backend_reason"] == factory_mod.DETECTION_PENDING


def test_cli_unopenable_collection_emits_backend_unavailable(tmp_path: Path) -> None:
    """End-to-end: an unopenable ``--col`` emits the envelope, no traceback.

    ``resolve_runtime_config``/``detect_backend`` run unstubbed: the regression
    was the fail-closed lock probe's ``sqlite3.OperationalError`` escaping
    ``detect_backend`` — ``app.py`` catches only ``DetectionError`` — so
    ``anki --backend direct --col <dir> decks`` crashed instead of emitting
    BACKEND_UNAVAILABLE. A directory passes ``exists()`` but
    ``sqlite3.connect`` cannot open it. ``decks`` is used because ``version``
    is backendless and skips detection entirely.
    """
    bad = tmp_path / "collection.anki2"
    bad.mkdir()

    runner = CliRunner()
    result = runner.invoke(
        app_mod.main,
        ["--backend", "direct", "--format", "json", "--col", str(bad), "decks"],
    )

    payload = _error_payload(result)
    assert result.exit_code == 7
    assert payload["error"]["code"] == "BACKEND_UNAVAILABLE"
    assert "Traceback" not in result.output


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
        ["commands"],
    ],
)
def test_backend_free_commands_skip_detection(monkeypatch, tmp_path: Path, argv) -> None:
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

    monkeypatch.setattr(app_mod, "detect_backend", _never_detect)

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
    # Not yet detected: the override is the best-known collection and the
    # requested backend stands in until a session resolves it.
    assert obj["collection_path"] == Path("/tmp/override.db")
    assert obj["backend"] == "direct"
    assert obj["backend_reason"] == factory_mod.DETECTION_PENDING


def test_first_session_detects_once_and_caches_the_result(monkeypatch, tmp_path: Path) -> None:
    """Two sessions in one process (the REPL opens one per action) probe once;
    the resolved backend/collection are written back for the envelope."""
    from tests.conftest import new_collection

    col = new_collection(tmp_path / "collection.anki2")
    calls: list[dict[str, Any]] = []

    def fake_detect(**kwargs: Any):
        calls.append(kwargs)
        return DetectionResult(backend="direct", collection_path=col.db_path, reason="forced")

    monkeypatch.setattr(factory_mod, "detect_backend", fake_detect)
    obj: dict[str, Any] = {
        "backend": "direct",
        "requested_backend": "direct",
        "backend_reason": factory_mod.DETECTION_PENDING,
        "collection_override": None,
        "app_config": AppConfig(),
    }

    with factory_mod.backend_session_from_context(obj) as first:
        pass
    with factory_mod.backend_session_from_context(obj) as second:
        pass

    assert len(calls) == 1
    assert first.name == second.name == "direct"
    assert obj["backend"] == "direct"
    assert obj["collection_path"] == col.db_path
    assert obj["backend_reason"] == "forced"


def test_failed_detection_is_cached_as_no_backend(monkeypatch) -> None:
    calls = 0

    def fail_detect(**kwargs: Any):
        nonlocal calls
        calls += 1
        raise DetectionError("no AnkiConnect and no collection", exit_code=3)

    monkeypatch.setattr(factory_mod, "detect_backend", fail_detect)
    obj: dict[str, Any] = {"backend": "auto", "backend_reason": factory_mod.DETECTION_PENDING}

    with pytest.raises(factory_mod.BackendFactoryError) as first:
        factory_mod.create_backend_from_context(obj)
    with pytest.raises(factory_mod.BackendFactoryError) as second:
        factory_mod.create_backend_from_context(obj)

    assert calls == 1
    assert first.value.exit_code == 3
    assert "no AnkiConnect" in str(first.value)
    assert obj["backend"] == "none"
    assert "no AnkiConnect" in str(second.value)
    assert second.value.exit_code == 3  # the cached failure keeps its meaning


def test_ankiconnect_version_from_detection_skips_the_second_probe(monkeypatch) -> None:
    """Detection already got ``version`` back; the backend must not ask again."""
    import anki_cli.backends.ankiconnect as ac_mod

    seen: dict[str, Any] = {}

    class FakeBackend:
        name = "ankiconnect"

        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(factory_mod, "AnkiConnectBackend", FakeBackend)
    FakeBackend.API_VERSION = ac_mod.AnkiConnectBackend.API_VERSION  # type: ignore[attr-defined]
    base: dict[str, Any] = {
        "backend": "ankiconnect",
        "backend_reason": "ankiconnect reachable",
        "collection_path": None,
    }

    factory_mod.create_backend_from_context({**base, "ankiconnect_version": 6})
    assert seen["verify_version"] is False

    factory_mod.create_backend_from_context({**base, "ankiconnect_version": 5})
    assert seen["verify_version"] is True  # too old: let the backend explain

    factory_mod.create_backend_from_context(dict(base))
    assert seen["verify_version"] is True  # hand-built obj, never probed


def test_bootstrap_forwards_anki_profile_to_detect(monkeypatch) -> None:
    # Pins the anki_profile= kwarg at the (now lazy) detect_backend call site.
    _install_dummy_command(monkeypatch, opens_session=True)

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

    monkeypatch.setattr(app_mod, "detect_backend", _never_detect)
    monkeypatch.setattr(factory_mod, "detect_backend", fake_detect)
    # Detection must be the only thing that ran: the fake path does not exist.
    monkeypatch.setattr(
        factory_mod, "DirectBackend", lambda path: types.SimpleNamespace(name="direct")
    )

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


@pytest.mark.parametrize("argv", [[], ["shell"]], ids=["bare", "shell"])
def test_repl_launch_detects_eagerly_and_warns_on_failure(monkeypatch, argv: list[str]) -> None:
    """Bare `anki` and `anki shell` both open the REPL on a host with no Anki;
    the header needs the resolved backend and the warning explains why backend
    commands will fail. Neither may take the lazy path (#32)."""
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
    monkeypatch.setattr(factory_mod, "detect_backend", _never_detect)

    runner = CliRunner()
    result = runner.invoke(app_mod.main, argv)

    assert result.exit_code == 0, result.output
    assert calls["obj"]["backend"] == "none"
    assert "no AnkiConnect" in calls["obj"]["backend_reason"]
    combined = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "warning:" in combined
    assert "backend commands unavailable" in combined


def test_envelope_before_any_session_reports_no_backend_not_the_preference(monkeypatch) -> None:
    """A command that fails input validation never opens a session, so the
    backend is unresolved; ``meta.backend`` must say "none", never "auto"."""

    @click.command("dummy")
    @click.pass_context
    def dummy_cmd(ctx: click.Context) -> None:
        from anki_cli.cli.formatter import formatter_from_ctx

        formatter_from_ctx(ctx).emit_error(command="dummy", code="INVALID_INPUT", message="bad")
        raise click.exceptions.Exit(2)

    monkeypatch.setattr(app_mod, "list_commands", lambda: ["dummy"])
    monkeypatch.setattr(app_mod, "get_command", lambda name: dummy_cmd if name == "dummy" else None)
    monkeypatch.setattr(
        app_mod, "resolve_runtime_config", lambda **kwargs: _runtime(backend="auto")
    )
    monkeypatch.setattr(app_mod, "detect_backend", _never_detect)
    monkeypatch.setattr(factory_mod, "detect_backend", _never_detect)

    result = CliRunner().invoke(app_mod.main, ["--format", "json", "dummy"])

    payload = _error_payload(result)
    assert payload["meta"]["backend"] == "none"


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
