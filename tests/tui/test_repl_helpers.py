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


@pytest.mark.parametrize(
    ("argv", "expected_obj", "expected_deck"),
    [
        (["deck", "--deck", "A", "--yes"], {"yes": True}, "A"),
        (["deck", "--yes", "--deck", "A"], {"yes": True}, "A"),
        (["deck", "--deck", "A", "--format", "JSON"], {"format": "json"}, "A"),
        (["deck", "--deck", "A", "--format=md", "--copy"], {"format": "md", "copy": True}, "A"),
        # A subcommand option's value that looks like a global flag stays a value.
        (["deck", "--deck", "--yes"], {"yes": False}, "--yes"),
        # A trailing flag must not swallow key=value sugar that follows it.
        (["deck", "--yes", "deck=A"], {"yes": True}, "A"),
        # Aliases resolve before the command lookup.
        (["d", "--deck", "A", "--yes"], {"yes": True}, "A"),
    ],
)
def test_invoke_command_accepts_global_options_after_the_command(
    monkeypatch: pytest.MonkeyPatch, argv, expected_obj, expected_deck
) -> None:
    """Same contract as the CLI (#26): ``--yes`` / ``--format`` may trail the
    command; they apply to that line only."""
    calls: dict[str, Any] = {}

    @click.command("deck")
    @click.option("--deck")
    @click.pass_context
    def cmd(ctx: click.Context, deck: str | None):
        calls["deck"] = deck
        calls["obj"] = dict(ctx.obj or {})

    monkeypatch.setattr(repl_mod, "get_command", lambda name: cmd if name == "deck" else None)
    monkeypatch.setitem(repl_mod._ALIASES, "d", "deck")
    session_obj = {"backend": "direct", "yes": False, "format": "table", "copy": False}

    repl_mod._invoke_command(session_obj, argv)

    assert calls["deck"] == expected_deck
    for key, value in expected_obj.items():
        assert calls["obj"][key] == value, key
    # The session object itself is untouched.
    assert session_obj == {"backend": "direct", "yes": False, "format": "table", "copy": False}


@pytest.mark.parametrize(
    "argv", [["--yess", "deck", "--deck", "A"], ["--backend", "direct", "deck"]]
)
def test_invoke_command_rejects_unknown_leading_option(
    monkeypatch: pytest.MonkeyPatch, capsys, argv
) -> None:
    """A mistyped (or session-fixed) leading option must not vanish silently
    while the command runs without it."""
    calls: dict[str, Any] = {}

    @click.command("deck")
    @click.option("--deck")
    def cmd(deck: str | None):
        calls["ran"] = True

    monkeypatch.setattr(repl_mod, "get_command", lambda name: cmd if name == "deck" else None)

    repl_mod._invoke_command({"yes": False}, argv)

    assert calls == {}
    assert f"No such option: {argv[0]}" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["deck", "--deck", "A", "--format"], "'--format' requires an argument"),
        (["deck", "--deck", "A", "--format", "yaml"], "Invalid value for '--format': 'yaml'"),
    ],
)
def test_invoke_command_reports_bad_trailing_format(
    monkeypatch: pytest.MonkeyPatch, capsys, argv, message
) -> None:
    calls: dict[str, Any] = {}

    @click.command("deck")
    @click.option("--deck")
    def cmd(deck: str | None):
        calls["ran"] = True

    monkeypatch.setattr(repl_mod, "get_command", lambda name: cmd if name == "deck" else None)

    repl_mod._invoke_command({"format": "table"}, argv)

    assert calls == {}
    assert message in capsys.readouterr().err


def test_show_command_help_unknown_prints_error(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(repl_mod, "get_command", lambda name: None)

    repl_mod._show_command_help("missing")

    captured = capsys.readouterr()
    assert "Unknown command: missing" in captured.err


# --- deck context for query commands (#28 follow-up) --------------------------------


@pytest.mark.parametrize(
    ("parts", "resolved", "expected"),
    [
        # query commands: fold the deck into --query
        (["c"], "cards", ["c", "--query", 'deck:"Japanese"']),
        (["cards", "--query", "is:due"], "cards", ["cards", "--query", 'deck:"Japanese" is:due']),
        (["notes", "--query=tag:x"], "notes", ["notes", '--query=deck:"Japanese" tag:x']),
        # an explicit deck: term wins
        (["c", "--query", "deck:Other"], "cards", ["c", "--query", "deck:Other"]),
        # --deck commands keep the old behaviour; commands with neither are untouched
        (["review:next"], "review:next", ["review:next", "--deck", "Japanese"]),
        (["review:next", "--deck", "X"], "review:next", ["review:next", "--deck", "X"]),
        (["version"], "version", ["version"]),
    ],
)
def test_apply_deck_context(monkeypatch: pytest.MonkeyPatch, parts, resolved, expected) -> None:
    @click.command("review:next")
    @click.option("--deck")
    def review_next(deck): ...

    @click.command("version")
    def version(): ...

    monkeypatch.setattr(
        repl_mod, "get_command", lambda n: {"review:next": review_next, "version": version}.get(n)
    )

    assert repl_mod._apply_deck_context(parts, resolved=resolved, deck="Japanese") == expected


@pytest.mark.parametrize(
    ("parts", "deck", "expected"),
    [
        (["c", "is:due"], None, ["c", "--query", "is:due"]),
        (["c", "tag:verb", "is:due"], None, ["c", "--query", "tag:verb is:due"]),
        (["ci", "is:new"], None, ["ci", "--query", "is:new"]),
        # already option-style: untouched
        (["c", "--query", "is:due"], None, ["c", "--query", "is:due"]),
        (["c", "--limit", "5"], None, ["c", "--limit", "5"]),
        # a non-query command keeps its positionals
        (["use", "Japanese"], None, ["use", "Japanese"]),
        # bare query + deck context compose
        (["c", "is:due"], "Japanese", ["c", "--query", 'deck:"Japanese" is:due']),
        ([], "Japanese", []),
    ],
)
def test_prepare_line(monkeypatch: pytest.MonkeyPatch, parts, deck, expected) -> None:
    monkeypatch.setattr(repl_mod, "get_command", lambda n: None)
    assert repl_mod._prepare_line(parts, deck_context=deck) == expected
