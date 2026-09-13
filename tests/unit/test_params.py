from __future__ import annotations

import click
import pytest

from anki_cli.cli.params import option_arity, preprocess_argv


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["note:add", "deck=Default", "Front=Q", "Back=A"],
            ["note:add", "--deck", "Default", "--Front", "Q", "--Back", "A"],
        ),
        (
            ["note:add", "--deck", "Default", "Front=Q"],
            ["note:add", "--deck", "Default", "--Front", "Q"],
        ),
        (
            ["note:add", "--", "Front=Q", "Back=A"],
            ["note:add", "--", "Front=Q", "Back=A"],
        ),
        (
            ["note:add", "--foo=bar", "Front=Q"],
            ["note:add", "--foo=bar", "--Front", "Q"],
        ),
        (
            ["note:add", "=value", "Front=Q"],
            ["note:add", "=value", "--Front", "Q"],
        ),
        (
            ["note:add", "bad key=value", "Front=Q"],
            ["note:add", "bad key=value", "--Front", "Q"],
        ),
        (
            ["note:add", "Front=a=b"],
            ["note:add", "--Front", "a=b"],
        ),
        (
            ["note:add", "Front="],
            ["note:add", "--Front", ""],
        ),
        (
            [],
            [],
        ),
    ],
)
def test_preprocess_argv_without_option_table(argv: list[str], expected: list[str]) -> None:
    """Legacy mode (no resolver): every key=value token is a candidate."""
    assert preprocess_argv(argv) == expected


# --- arity-aware mode (#25) ----------------------------------------------------

_GROUP = click.Group(
    "anki",
    params=[
        click.Option(["--format", "output_format"]),
        click.Option(["--col", "collection_path"]),
        click.Option(["--yes"], is_flag=True),
    ],
)
_CARDS_IDS = click.Command("cards:ids", params=[click.Option(["--query", "-q"])])
_NOTE_ADD = click.Command(
    "note:add",
    params=[
        click.Option(["--deck"]),
        click.Option(["--notetype"]),
        click.Option(["--allow-duplicate"], is_flag=True),
    ],
)
_CONFIG_SET = click.Command(
    "config:set", params=[click.Option(["--key"]), click.Option(["--value"])]
)
_EXOTIC = click.Command(
    "exotic",
    params=[
        click.Option(["-v", "--verbose"], count=True),
        click.Option(["-q", "--query"]),
        click.Option(["--range"], nargs=2),
    ],
)
_COMMANDS = {c.name: c for c in (_CARDS_IDS, _NOTE_ADD, _CONFIG_SET, _EXOTIC)}


def _resolve(name: str):
    cmd = _COMMANDS.get(name)
    return option_arity(cmd) if cmd is not None else None


def _pp(argv: list[str]) -> list[str]:
    return preprocess_argv(
        argv, group_options=option_arity(_GROUP), resolve_command_options=_resolve
    )


def test_option_arity_counts_tokens_consumed() -> None:
    arity = option_arity(_NOTE_ADD)
    assert arity == {
        "-h": 0,
        "--help": 0,
        "--deck": 1,
        "--notetype": 1,
        "--allow-duplicate": 0,
    }
    assert option_arity(_CARDS_IDS) == {"-h": 0, "--help": 0, "--query": 1, "-q": 1}
    assert option_arity(None) == {}

    exotic = click.Command(
        "x",
        params=[
            click.Option(["-v", "--verbose"], count=True),  # Click consumes nothing
            click.Option(["--range"], nargs=2),
            click.Option(["--on/--off"]),
            click.Option(["--field"], multiple=True),
        ],
    )
    assert option_arity(exotic) == {
        "-h": 0,
        "--help": 0,
        "-v": 0,
        "--verbose": 0,
        "--range": 2,
        "--on": 0,
        "--off": 0,
        "--field": 1,
    }


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        # The SKILL.md example that used to break: value of --query is not sugar.
        (
            ["cards:ids", "--query", "prop:lapses=0"],
            ["cards:ids", "--query", "prop:lapses=0"],
        ),
        (
            ["cards:ids", "-q", "prop:ivl>=10 deck:a=b"],
            ["cards:ids", "-q", "prop:ivl>=10 deck:a=b"],
        ),
        # Field values and config values containing '=' survive.
        (
            ["note:add", "--deck", "Lang=Spanish", "Front=a=b"],
            ["note:add", "--deck", "Lang=Spanish", "--Front", "a=b"],
        ),
        (
            ["config:set", "--key", "x", "--value", "a=b"],
            ["config:set", "--key", "x", "--value", "a=b"],
        ),
        # Group options before the subcommand are consulted too.
        (
            ["--col", "path=with=eq.anki2", "cards:ids"],
            ["--col", "path=with=eq.anki2", "cards:ids"],
        ),
        (
            ["--format", "json", "note:add", "Front=Q"],
            ["--format", "json", "note:add", "--Front", "Q"],
        ),
        # A flag does not swallow the next token, so sugar after it still applies.
        (
            ["note:add", "--allow-duplicate", "Front=Q"],
            ["note:add", "--allow-duplicate", "--Front", "Q"],
        ),
        (["--yes", "note:add", "Front=Q"], ["--yes", "note:add", "--Front", "Q"]),
        # --opt=value carries its own value.
        (["note:add", "--deck=A=B", "Front=Q"], ["note:add", "--deck=A=B", "--Front", "Q"]),
        # Sugar for the command's own valued option still works and keeps inner '='.
        (["cards:ids", "query=prop:lapses=0"], ["cards:ids", "--query", "prop:lapses=0"]),
        # Unknown option: assume it takes a value rather than mangle it (Click errors on it).
        (["note:add", "--typo", "Front=Q"], ["note:add", "--typo", "Front=Q"]),
        # Unknown command: nothing after it can be resolved; legacy behaviour.
        (["nope", "a=b"], ["nope", "--a", "b"]),
        # count / nargs=2 / short-attached / flag-cluster follow Click's consumption.
        (["exotic", "-v", "Front=Q"], ["exotic", "-v", "--Front", "Q"]),
        (["exotic", "--range", "1", "a=b", "c=d"], ["exotic", "--range", "1", "a=b", "--c", "d"]),
        (["exotic", "-qdeck:x", "Front=Q"], ["exotic", "-qdeck:x", "--Front", "Q"]),
        (["exotic", "-vq", "prop:x=1", "Front=Q"], ["exotic", "-vq", "prop:x=1", "--Front", "Q"]),
        (["exotic", "-h", "Front=Q"], ["exotic", "-h", "--Front", "Q"]),
        # Bare '-' is a positional, not an option.
        (["note:add", "-", "Front=Q"], ["note:add", "-", "--Front", "Q"]),
        # '--' terminates processing.
        (["note:add", "--", "--query", "a=b"], ["note:add", "--", "--query", "a=b"]),
    ],
)
def test_preprocess_argv_respects_option_arity(argv: list[str], expected: list[str]) -> None:
    assert _pp(argv) == expected
