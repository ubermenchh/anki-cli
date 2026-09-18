"""#28 item 4: one spelling per concept everywhere (``--deck``, ``--notetype``,
``--tag``), with the older spellings kept as aliases so existing scripts work."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import pytest
from click.testing import CliRunner

import anki_cli.cli.commands.deck as deck_mod
import anki_cli.cli.commands.notetype as notetype_mod
import anki_cli.cli.commands.tag as tag_mod


def _obj() -> dict[str, Any]:
    return {"format": "json", "backend": "direct", "collection_path": None,
            "no_color": True, "copy": False, "yes": True}


def _patch(monkeypatch: pytest.MonkeyPatch, module: Any, backend: Any) -> None:
    @contextmanager
    def fake_session(obj: dict[str, Any]):
        yield backend

    monkeypatch.setattr(module, "backend_session_from_context", fake_session)


@pytest.mark.parametrize("spelling", ["--deck", "--name"])
def test_deck_create_accepts_deck_and_name(monkeypatch, spelling) -> None:
    seen: dict[str, Any] = {}

    class Backend:
        def create_deck(self, name: str) -> dict[str, Any]:
            seen["name"] = name
            return {"name": name, "id": 1}

    _patch(monkeypatch, deck_mod, Backend())
    result = CliRunner().invoke(deck_mod.deck_create_cmd, [spelling, "Lang::Es"], obj=_obj())

    assert result.exit_code == 0, result.output
    assert seen["name"] == "Lang::Es"


@pytest.mark.parametrize("spelling", ["--notetype", "--name"])
def test_notetype_create_accepts_notetype_and_name(monkeypatch, spelling) -> None:
    seen: dict[str, Any] = {}

    class Backend:
        def create_notetype(self, **kwargs: Any) -> dict[str, Any]:
            seen.update(kwargs)
            return {"name": kwargs["name"], "id": 1}

    _patch(monkeypatch, notetype_mod, Backend())
    result = CliRunner().invoke(
        notetype_mod.notetype_create_cmd,
        [spelling, "MyType", "--field", "Front", "--field", "Back"],
        obj=_obj(),
    )

    assert result.exit_code == 0, result.output
    assert seen["name"] == "MyType"


@pytest.mark.parametrize("spelling", ["--tag", "--tags"])
def test_tag_add_accepts_tag_and_tags(monkeypatch, spelling) -> None:
    seen: dict[str, Any] = {}

    class Backend:
        def find_notes(self, query: str) -> list[int]:
            return [1]

        def add_tags(self, note_ids: list[int], tags: list[str]) -> dict[str, Any]:
            seen["tags"] = tags
            return {"count": len(note_ids)}

    _patch(monkeypatch, tag_mod, Backend())
    result = CliRunner().invoke(
        tag_mod.tag_add_cmd, ["--query", "deck:X", spelling, "a b"], obj=_obj()
    )

    assert result.exit_code == 0, result.output
    assert seen["tags"] == ["a", "b"]


def test_commands_introspection_lists_every_registered_command() -> None:
    from anki_cli.cli.commands.general import commands_cmd
    from anki_cli.cli.dispatcher import list_commands

    result = CliRunner().invoke(commands_cmd, [], obj=_obj())

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    names = [i["name"] for i in payload["data"]["items"]]
    assert names == list_commands()
    by_name = {i["name"]: i for i in payload["data"]["items"]}
    assert by_name["search"]["hidden"] is True
    assert "--query" in by_name["cards"]["options"][0]["spellings"]
    assert payload["data"]["exit_codes"]["2"].startswith("Invalid input")
    assert "INVALID_INPUT" in payload["data"]["error_codes"]
