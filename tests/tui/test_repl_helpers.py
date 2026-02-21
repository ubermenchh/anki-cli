from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

import click
import pytest

pytest.importorskip("prompt_toolkit")
pytest.importorskip("markdownify")

import anki_cli.backends.factory as factory_mod
import anki_cli.tui.repl as repl_mod

pytestmark = pytest.mark.tui


def test_strip_html_basic() -> None:
    out = repl_mod._strip_html("<p>Hello</p><p>World</p>")
    assert "Hello" in out
    assert "World" in out


def test_history_path_uses_xdg_data_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    path = repl_mod._history_path()

    assert path == (tmp_path / "xdg" / "anki-cli" / "repl_history")
    assert path.parent.exists()


def test_history_path_falls_back_to_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    path = repl_mod._history_path()

    assert path == (home / ".local" / "share" / "anki-cli" / "repl_history")
    assert path.parent.exists()


def test_due_counts_inline_formats_values() -> None:
    assert repl_mod._due_counts_inline({}) == ""
    assert repl_mod._due_counts_inline(
        {"new": 2, "learn": 3, "review": 4}
    ) == "new=2 learn=3 review=4"


def test_fetch_due_counts_success_trims_deck(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    class Backend:
        def get_due_counts(self, deck: str | None = None) -> dict[str, int]:
            calls["deck"] = deck
            return {"new": 5, "learn": 6, "review": 7, "total": 18}

    @contextmanager
    def fake_session(obj: dict[str, Any]):
        yield Backend()

    monkeypatch.setattr(factory_mod, "backend_session_from_context", fake_session)

    out = repl_mod._fetch_due_counts({"backend": "direct"}, "  DeckA  ")

    assert out == {"new": 5, "learn": 6, "review": 7}
    assert calls["deck"] == "DeckA"


def test_fetch_due_counts_failure_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_session(obj: dict[str, Any]):
        raise RuntimeError("boom")

    monkeypatch.setattr(factory_mod, "backend_session_from_context", failing_session)

    out = repl_mod._fetch_due_counts({"backend": "direct"}, "DeckA")
    assert out == {}


def test_completer_options_for_alias_and_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}

    cmd = click.Command(
        "deck",
        params=[
            click.Option(["--deck"]),
            click.Option(["-q", "--query"]),
        ],
    )

    def fake_get_command(name: str):
        calls["count"] += 1
        return cmd if name == "deck" else None

    monkeypatch.setattr(repl_mod, "get_command", fake_get_command)

    comp = repl_mod._AnkiCompleter()
    first = comp._options_for("dk")  # alias -> deck
    second = comp._options_for("dk")  # from cache

    assert "--deck" in first
    assert "-q" in first
    assert "--query" in first
    assert second == first
    assert calls["count"] == 1


def test_completer_command_help_first_line_and_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    help_text = """
    This is a very long first line that should be truncated by the completer output.\nSecond line.
    """
    cmd = click.Command("x", help=help_text)

    monkeypatch.setattr(repl_mod, "get_command", lambda name: cmd if name == "x" else None)

    comp = repl_mod._AnkiCompleter()
    out = comp._command_help("x")

    assert len(out) <= 50
    assert out.startswith("This is a very long first line")


def test_invoke_command_unknown_command_prints_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys
) -> None:
    monkeypatch.setattr(repl_mod, "get_command", lambda name: None)

    repl_mod._invoke_command({"backend": "direct"}, ["nope"])

    captured = capsys.readouterr()
    assert "Unknown command: nope" in captured.err


def test_invoke_command_calls_click_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    @click.command("deck")
    @click.option("--deck")
    @click.pass_context
    def cmd(ctx: click.Context, deck: str | None):
        calls["deck"] = deck
        calls["obj"] = dict(ctx.obj or {})

    monkeypatch.setattr(repl_mod, "get_command", lambda name: cmd if name == "deck" else None)

    repl_mod._invoke_command({"backend": "direct"}, ["deck", "--deck", "A"])

    assert calls["deck"] == "A"
    assert calls["obj"]["backend"] == "direct"


def test_show_command_help_unknown_prints_error(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(repl_mod, "get_command", lambda name: None)

    repl_mod._show_command_help("missing")

    captured = capsys.readouterr()
    assert "Unknown command: missing" in captured.err