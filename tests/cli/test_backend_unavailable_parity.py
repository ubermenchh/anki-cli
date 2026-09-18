"""Every backend-using command reports a session-open failure the same way:
``BACKEND_UNAVAILABLE`` / exit 7 / ``{"backend": ...}`` (#29 parity oracle).

The #67 review found `note:bulk` had silently become exit 1 because its own
``RuntimeError`` row shadowed the backend-unavailable check, and no test covered
a factory failure on that command. This drives the *whole registry* so the gap
cannot reopen for any command.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from anki_cli.backends.factory import BackendFactoryError
from anki_cli.cli.dispatcher import get_command, list_commands

# Commands that never open a backend session (or open it in a way that is not
# the factory): nothing to assert.
_NO_SESSION = {"version", "status", "commands", "config", "config:path", "config:set", "shell"}

# Minimal valid argv per command so option validation passes and the body
# reaches the backend. --yes is set in ctx.obj for the destructive ones.
_ARGV: dict[str, list[str]] = {
    "card": ["--id", "1"],
    "card:revlog": ["--id", "1"],
    "card:suspend": ["--id", "1"],
    "card:unsuspend": ["--id", "1"],
    "card:move": ["--id", "1", "--deck", "D"],
    "card:flag": ["--id", "1", "--flag", "1"],
    "card:bury": ["--id", "1"],
    "card:reschedule": ["--id", "1", "--days", "1"],
    "card:reset": ["--id", "1"],
    "deck": ["--deck", "D"],
    "deck:create": ["--deck", "D"],
    "deck:rename": ["--from", "A", "--to", "B"],
    "deck:delete": ["--deck", "D"],
    "deck:config": ["--deck", "D"],
    "deck:config:set": ["--deck", "D", "--new-per-day", "1"],
    "note": ["--id", "1"],
    "note:add": ["--deck", "D", "--notetype", "B", "--Front", "Q"],
    "note:edit": ["--id", "1", "--Front", "Q"],
    "note:delete": ["--id", "1"],
    "note:bulk": ["--deck", "D", "--notetype", "B"],
    "note:fields": ["--id", "1"],
    "notetype": ["--notetype", "B"],
    "notetype:create": ["--notetype", "B"],
    "notetype:field:add": ["--notetype", "B", "--field", "F"],
    "notetype:field:remove": ["--notetype", "B", "--field", "F"],
    "notetype:template:add": ["--notetype", "B", "--template", "T", "--front", "f", "--back", "b"],
    "notetype:template:edit": ["--notetype", "B", "--template", "T", "--front", "f"],
    "notetype:css": ["--notetype", "B"],
    "tag": ["--tag", "t"],
    "tag:add": ["--id", "1", "--tag", "t"],
    "tag:remove": ["--id", "1", "--tag", "t"],
    "tag:rename": ["--from", "a", "--to", "b"],
    "review:preview": ["--id", "1"],
    "review:answer": ["--id", "1", "--rating", "good"],
    "search": ["--query", "x"],
}
_STDIN = {"note:bulk": '[{"Front": "Q"}]'}


def _obj() -> dict[str, Any]:
    return {"format": "json", "backend": "direct", "collection_path": None,
            "no_color": True, "copy": False, "yes": True}


def test_argv_table_covers_every_command_with_a_required_option() -> None:
    """Adding a command with required options without adding it here fails loudly
    (a command with only optional options runs fine on empty argv)."""
    import click

    needs_argv = [
        n
        for n in list_commands()
        if n not in _NO_SESSION
        and any(isinstance(p, click.Option) and p.required for p in get_command(n).params)  # type: ignore[union-attr]
    ]
    missing = [n for n in needs_argv if n not in _ARGV]
    assert not missing, f"add argv for: {missing}"


@pytest.mark.parametrize("name", [n for n in list_commands() if n not in _NO_SESSION])
def test_session_open_failure_is_backend_unavailable(monkeypatch, name: str) -> None:
    import anki_cli.backends.factory as factory_mod

    cmd = get_command(name)
    assert cmd is not None

    def boom(obj: dict[str, Any]):
        raise BackendFactoryError("backend down")

    monkeypatch.setattr(factory_mod, "backend_session_from_context", boom)

    result = CliRunner().invoke(cmd, _ARGV.get(name, []), input=_STDIN.get(name), obj=_obj())

    if result.exit_code == 2 and name in {"browse", "review:start"}:
        # TUI extras may not be installed; that is TUI_NOT_AVAILABLE, not a
        # session failure. Skip rather than fake the import.
        pytest.skip("TUI not installed")
    payload = json.loads(result.stderr)
    assert result.exit_code == 7, (name, payload)
    assert payload["error"]["code"] == "BACKEND_UNAVAILABLE", name
    assert payload["error"]["details"] == {"backend": "direct"}, name
    assert payload["meta"]["command"] == name
