"""Entry-point guarantees (#26, #27): every exit goes through the envelope, and
group options are accepted after the subcommand.

These drive ``app_mod.main`` through ``CliRunner`` exactly as the console
script does, with bootstrap (config + detection) stubbed so only the
entry-point behaviour is under test.
"""

from __future__ import annotations

import json
import sqlite3
import types
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

import anki_cli.cli.app as app_mod
from anki_cli.backends.ankiconnect import AnkiConnectUnavailableError
from anki_cli.backends.detect import DetectionResult
from anki_cli.db.errors import DirectWriteBlockedError
from anki_cli.models.config import AppConfig


def _runtime(output_format: str = "json"):
    return types.SimpleNamespace(
        app=AppConfig(),
        config_path=Path("/tmp/config.toml"),
        backend="direct",
        output_format=output_format,
        no_color=True,
        collection_override=None,
        warnings=[],
    )


def _install(monkeypatch: pytest.MonkeyPatch, *commands: click.Command, fmt: str = "json") -> None:
    by_name = {cmd.name: cmd for cmd in commands}
    monkeypatch.setattr(app_mod, "list_commands", lambda: sorted(by_name))
    monkeypatch.setattr(app_mod, "get_command", by_name.get)
    def _resolve(**kw: Any):
        # Like the real resolver: an explicit CLI --format wins over the default.
        chosen = kw["cli_output_format"] if kw.get("cli_output_set") else fmt
        return _runtime(str(chosen).lower())

    monkeypatch.setattr(app_mod, "resolve_runtime_config", _resolve)
    monkeypatch.setattr(
        app_mod,
        "detect_backend",
        lambda **kw: DetectionResult(backend="direct", collection_path=None, reason="forced"),
    )


def _envelope(result) -> dict[str, Any]:
    # Envelopes go to stderr only; parsing stderr alone means a stray stdout
    # line fails the test cleanly instead of as a JSONDecodeError.
    payload = json.loads(result.stderr.strip())
    assert payload["ok"] is False
    return payload


def _raising(name: str, exc: BaseException) -> click.Command:
    @click.command(name)
    @click.option("--id", type=int, default=None)
    def cmd(id: int | None) -> None:
        raise exc

    return cmd


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# --- #27: Click's own errors are enveloped ---------------------------------------


def test_unknown_option_after_subcommand_is_a_json_envelope(monkeypatch, runner) -> None:
    """The issue's headline case: usage errors used to be plain text, exit 2,
    ignoring --format json."""
    _install(monkeypatch, _raising("probe", AssertionError("not reached")))

    result = runner.invoke(app_mod.main, ["--format", "json", "probe", "--bogus"])

    payload = _envelope(result)
    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert "No such option: --bogus" in payload["error"]["message"]
    assert payload["error"]["details"]["usage"].startswith("Usage:")
    assert payload["meta"]["command"] == "probe"
    assert payload["meta"]["backend"] == "direct"


@pytest.mark.parametrize("fmt", ["json", "JSON", "Json"])
def test_unknown_command_is_a_json_envelope_using_argv_format(monkeypatch, runner, fmt) -> None:
    """No ctx.obj exists yet for an unknown command; --format is read off argv,
    case-insensitively like the option itself."""
    _install(monkeypatch, _raising("probe", AssertionError("not reached")))

    result = runner.invoke(app_mod.main, ["--format", fmt, "nope:cmd"])

    payload = _envelope(result)
    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert "No such command 'nope:cmd'" in payload["error"]["message"]
    assert payload["meta"]["command"] == "bootstrap"
    assert payload["meta"]["backend"] == "none"


def test_unknown_command_table_format_is_plain_text(monkeypatch, runner) -> None:
    _install(monkeypatch, _raising("probe", AssertionError("not reached")), fmt="table")

    result = runner.invoke(app_mod.main, ["nope"])

    assert result.exit_code == 2
    assert result.output.startswith("INVALID_INPUT: No such command 'nope'")


def test_bad_group_option_value_is_enveloped(monkeypatch, runner) -> None:
    _install(monkeypatch, _raising("probe", AssertionError("not reached")))

    result = runner.invoke(app_mod.main, ["--format", "json", "--backend", "bogus", "probe"])

    payload = _envelope(result)
    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert "--backend" in payload["error"]["message"]


def test_help_and_version_still_exit_zero_with_plain_output(monkeypatch, runner) -> None:
    _install(monkeypatch, _raising("probe", AssertionError("not reached")))

    for argv in (["--help"], ["probe", "--help"], ["probe", "-h"], ["--version"]):
        result = runner.invoke(app_mod.main, argv)
        assert result.exit_code == 0, (argv, result.output)
        assert not result.output.startswith("{")

    # Help after the subcommand is the *subcommand's* help (not hoisted).
    result = runner.invoke(app_mod.main, ["probe", "--help"])
    assert "--id" in result.output
    # --version stays put too, so it is an error for a subcommand as before.
    result = runner.invoke(app_mod.main, ["--format", "json", "probe", "--version"])
    assert result.exit_code == 2
    assert "No such option: --version" in _envelope(result)["error"]["message"]


# --- #27: exceptions escaping a command are enveloped ---------------------------


@pytest.mark.parametrize(
    ("exc", "code", "exit_code"),
    [
        (AnkiConnectUnavailableError("connection dropped"), "BACKEND_UNAVAILABLE", 7),
        (DirectWriteBlockedError("Anki Desktop is running"), "COLLECTION_LOCKED", 7),
        (sqlite3.OperationalError("database is locked"), "COLLECTION_LOCKED", 7),
        (sqlite3.OperationalError("no such table: foo"), "BACKEND_OPERATION_FAILED", 1),
        (LookupError("Card not found: 7"), "ENTITY_NOT_FOUND", 4),
        (ValueError("bad"), "BACKEND_OPERATION_FAILED", 1),
        (NotImplementedError("ankiconnect cannot"), "BACKEND_UNAVAILABLE", 7),
        (RuntimeError("boom"), "INTERNAL_ERROR", 1),
        (ZeroDivisionError("x"), "INTERNAL_ERROR", 1),
    ],
    ids=lambda v: type(v).__name__ if isinstance(v, BaseException) else str(v),
)
@pytest.mark.parametrize(
    "argv",
    [["--format", "json", "probe"], ["probe"]],
    ids=["format-on-argv", "format-from-config"],
)
def test_escaping_exception_becomes_envelope(
    monkeypatch, runner, exc, code, exit_code, argv
) -> None:
    """Domain exceptions carry no Click ctx; the envelope must still use the
    format and backend the group callback resolved (the ``["probe"]`` case
    only knows about JSON through the stubbed config)."""
    _install(monkeypatch, _raising("probe", exc))  # fmt="json" = config says json

    result = runner.invoke(app_mod.main, argv)

    payload = _envelope(result)
    assert result.exit_code == exit_code
    assert payload["error"]["code"] == code
    assert str(exc) in payload["error"]["message"] or code == "INTERNAL_ERROR"
    assert payload["meta"]["command"] == "probe"
    assert payload["meta"]["backend"] == "direct"  # resolved runtime, not the argv peek
    assert "Traceback" not in result.stderr


def test_internal_error_names_the_exception_type(monkeypatch, runner) -> None:
    _install(monkeypatch, _raising("probe", RuntimeError("boom")))

    result = runner.invoke(app_mod.main, ["--format", "json", "probe"])

    payload = _envelope(result)
    assert payload["error"]["details"] == {"exception": "RuntimeError"}
    assert "RuntimeError: boom" in payload["error"]["message"]


def test_debug_env_adds_traceback_after_envelope(monkeypatch, runner) -> None:
    _install(monkeypatch, _raising("probe", RuntimeError("boom")))
    monkeypatch.setenv("ANKI_CLI_DEBUG", "1")

    result = runner.invoke(app_mod.main, ["--format", "json", "probe"])

    assert result.exit_code == 1
    assert '"code": "INTERNAL_ERROR"' in result.stderr
    assert "Traceback (most recent call last)" in result.stderr


def test_keyboard_interrupt_is_exit_130(monkeypatch, runner) -> None:
    _install(monkeypatch, _raising("probe", KeyboardInterrupt()))

    result = runner.invoke(app_mod.main, ["--format", "json", "probe"])

    payload = _envelope(result)
    assert result.exit_code == 130
    assert payload["error"]["code"] == "INTERRUPTED"


def test_command_emitted_envelope_passes_through_untouched(monkeypatch, runner) -> None:
    """A command that already emitted its own envelope and raised Exit(n) must
    not get a second envelope from the entry point."""

    @click.command("probe")
    @click.pass_context
    def probe(ctx: click.Context) -> None:
        click.echo('{"ok": false, "error": {"code": "ENTITY_NOT_FOUND"}}', err=True)
        raise click.exceptions.Exit(4)

    _install(monkeypatch, probe)

    result = runner.invoke(app_mod.main, ["--format", "json", "probe"])

    assert result.exit_code == 4
    assert result.stderr.count('"ok": false') == 1


def test_sys_exit_from_command_passes_through_unenveloped(monkeypatch, runner) -> None:
    _install(monkeypatch, _raising("probe", SystemExit(3)))

    result = runner.invoke(app_mod.main, ["--format", "json", "probe"])

    assert result.exit_code == 3
    assert '"ok"' not in result.stderr


def test_group_level_usage_error_is_attributed_to_the_named_command(monkeypatch, runner) -> None:
    """A bad *group* option value fires before any command ran, but meta.command
    still names the command the user was trying to run (read off argv)."""
    _install(monkeypatch, _raising("probe", AssertionError("not reached")))

    result = runner.invoke(app_mod.main, ["--format", "json", "--backend", "bogus", "probe"])

    payload = _envelope(result)
    assert payload["meta"]["command"] == "probe"


def test_unknown_first_positional_is_not_attributed_to_a_later_command(monkeypatch, runner) -> None:
    _install(monkeypatch, _raising("probe", AssertionError("not reached")))

    result = runner.invoke(app_mod.main, ["--format", "json", "nope", "probe"])

    payload = _envelope(result)
    assert "No such command 'nope'" in payload["error"]["message"]
    assert payload["meta"]["command"] == "bootstrap"


def test_typo_option_followed_by_trailing_format_still_yields_json(monkeypatch, runner) -> None:
    """The documented agent pattern is ``anki cmd ... --format json``; one typo in
    front of it must not turn the usage error back into plain text."""
    _install(monkeypatch, _raising("probe", AssertionError("not reached")), fmt="table")

    result = runner.invoke(app_mod.main, ["probe", "--bogus", "--format", "json"])

    payload = _envelope(result)
    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert "No such option: --bogus" in payload["error"]["message"]


def test_dangling_trailing_group_option_is_a_usage_error(monkeypatch, runner) -> None:
    """``note:delete --id 1 --format`` must not read the command name as the
    format value."""
    _install(monkeypatch, _raising("probe", AssertionError("not reached")))

    result = runner.invoke(app_mod.main, ["--format", "json", "probe", "--id", "1", "--format"])

    payload = _envelope(result)
    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert "'--format' requires an argument" in payload["error"]["message"]


def test_error_inside_root_parse_does_not_reuse_previous_ctx(monkeypatch, runner) -> None:
    """``_root_ctx`` lives on the module-level group; a failure before this
    invocation's context exists must not render with the previous one's."""

    @click.command("probe")
    def probe() -> None:
        click.echo("ran")

    _install(monkeypatch, probe)
    assert runner.invoke(app_mod.main, ["--format", "json", "probe"]).exit_code == 0

    def boom(self, ctx, args):
        raise RuntimeError("parse blew up")

    monkeypatch.setattr(type(app_mod.main), "parse_args", boom)
    result = runner.invoke(app_mod.main, ["--format", "json", "probe"])

    payload = _envelope(result)
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert payload["meta"]["backend"] == "none"  # not the seeded "direct"
    assert payload["meta"]["command"] == "probe"


def test_successful_command_exit_zero(monkeypatch, runner) -> None:
    @click.command("probe")
    def probe() -> None:
        click.echo("ran")

    _install(monkeypatch, probe)

    result = runner.invoke(app_mod.main, ["probe"])

    assert result.exit_code == 0
    assert result.output.strip() == "ran"


# --- #26: group options after the subcommand -------------------------------------


def _capturing_probe() -> tuple[click.Command, dict[str, Any]]:
    seen: dict[str, Any] = {}

    @click.command("probe")
    @click.option("--id", type=int, default=None)
    @click.option("--query", default=None)
    @click.pass_context
    def probe(ctx: click.Context, id: int | None, query: str | None) -> None:
        seen.update(obj=dict(ctx.obj), id=id, query=query)
        click.echo("ran")

    return probe, seen


@pytest.mark.parametrize(
    "argv",
    [
        ["probe", "--id", "1", "--yes"],
        ["probe", "--yes", "--id", "1"],
        ["--yes", "probe", "--id", "1"],
        ["probe", "--id", "1", "--format", "json", "--yes"],
        ["probe", "--id", "1", "--format=json", "--yes"],
        ["probe", "id=1", "--yes"],
    ],
)
def test_yes_and_format_accepted_after_subcommand(monkeypatch, runner, argv) -> None:
    probe, seen = _capturing_probe()
    _install(monkeypatch, probe, fmt="table")

    result = runner.invoke(app_mod.main, argv)

    assert result.exit_code == 0, (argv, result.output)
    assert seen["obj"]["yes"] is True
    assert seen["id"] == 1
    if "--format" in argv or "--format=json" in argv:
        assert seen["obj"]["format"] == "json"


def test_hoisted_option_value_is_not_swallowed_by_subcommand(monkeypatch, runner) -> None:
    """``--query`` belongs to the subcommand, ``--backend`` to the group; each
    must keep its own value token."""
    probe, seen = _capturing_probe()
    _install(monkeypatch, probe, fmt="table")

    result = runner.invoke(
        app_mod.main, ["probe", "--query", "--yes", "--backend", "direct", "--id", "2"]
    )

    assert result.exit_code == 0, result.output
    assert seen["query"] == "--yes"  # consumed as --query's value, not hoisted
    assert seen["obj"]["yes"] is False
    assert seen["id"] == 2


def test_subcommand_owned_spelling_is_not_hoisted(monkeypatch, runner) -> None:
    """If a subcommand defines its own ``--copy``, the group must not steal it."""
    seen: dict[str, Any] = {}

    @click.command("probe")
    @click.option("--copy", "copy_target", default=None)
    @click.pass_context
    def probe(ctx: click.Context, copy_target: str | None) -> None:
        seen.update(copy_target=copy_target, group_copy=ctx.obj["copy"])

    _install(monkeypatch, probe, fmt="table")

    result = runner.invoke(app_mod.main, ["probe", "--copy", "there"])

    assert result.exit_code == 0, result.output
    assert seen == {"copy_target": "there", "group_copy": False}


def test_tokens_after_double_dash_are_not_hoisted(monkeypatch, runner) -> None:
    @click.command("probe")
    @click.argument("words", nargs=-1)
    @click.pass_context
    def probe(ctx: click.Context, words: tuple[str, ...]) -> None:
        click.echo(json.dumps({"words": list(words), "yes": ctx.obj["yes"]}))

    _install(monkeypatch, probe, fmt="table")

    result = runner.invoke(app_mod.main, ["probe", "--", "--yes", "x"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"words": ["--yes", "x"], "yes": False}
