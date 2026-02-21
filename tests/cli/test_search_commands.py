from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

from click.testing import CliRunner

import anki_cli.cli.commands.search as search_cmd_mod
from anki_cli.backends.ankiconnect import AnkiConnectAPIError
from anki_cli.backends.factory import BackendFactoryError
from anki_cli.cli.commands.search import search_cmd
from anki_cli.cli.dispatcher import get_command
from anki_cli.core.search import SearchParseError


def _base_obj(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "format": "json",
        "backend": "direct",
        "collection_path": None,
        "no_color": True,
        "copy": False,
    }
    base.update(overrides)
    return base


def _success_payload(result) -> dict[str, Any]:
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    return payload


def _error_payload(result) -> dict[str, Any]:
    assert result.exit_code != 0
    raw = (getattr(result, "stderr", "") or result.output).strip()
    payload = json.loads(raw)
    assert payload["ok"] is False
    return payload


def _patch_session(monkeypatch, backend: Any) -> None:
    @contextmanager
    def fake_session(obj: dict[str, Any]):
        yield backend

    monkeypatch.setattr(search_cmd_mod, "backend_session_from_context", fake_session)


def test_search_cmd_success(monkeypatch) -> None:
    calls: dict[str, Any] = {}

    class Backend:
        def find_cards(self, query: str) -> list[int]:
            calls["query"] = query
            return [101, 102]

        def get_card(self, card_id: int) -> dict[str, Any]:
            return {"id": card_id, "queue": 2}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(search_cmd, ["--query", "deck:Default"], obj=_base_obj())
    payload = _success_payload(result)

    assert payload["meta"]["command"] == "search"
    assert payload["data"] == {
        "query": "deck:Default",
        "count": 2,
        "items": [{"id": 101, "queue": 2}, {"id": 102, "queue": 2}],
    }
    assert calls["query"] == "deck:Default"


def test_search_cmd_backend_unavailable_exit_7(monkeypatch) -> None:
    def failing_session(obj: dict[str, Any]):
        raise BackendFactoryError("backend down")

    monkeypatch.setattr(search_cmd_mod, "backend_session_from_context", failing_session)

    runner = CliRunner()
    result = runner.invoke(search_cmd, ["--query", "x"], obj=_base_obj(backend="direct"))
    payload = _error_payload(result)

    assert result.exit_code == 7
    assert payload["error"]["code"] == "BACKEND_UNAVAILABLE"
    assert payload["error"]["details"] == {"backend": "direct"}


def test_search_command_registered() -> None:
    assert get_command("search") is not None

def test_search_cmd_invalid_query_parse_error_exit_2(monkeypatch) -> None:
    class Backend:
        def find_cards(self, query: str) -> list[int]:
            raise SearchParseError("Unexpected token", query=query, position=1)

        def get_card(self, card_id: int) -> dict[str, Any]:
            raise AssertionError("get_card should not be called on invalid query")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(search_cmd, ["--query", "("], obj=_base_obj())
    payload = _error_payload(result)

    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert payload["error"]["details"]["query"] == "("
    assert payload["error"]["details"]["position"] == 1


def test_search_cmd_invalid_query_ankiconnect_error_exit_2(monkeypatch) -> None:
    class Backend:
        def find_cards(self, query: str) -> list[int]:
            raise AnkiConnectAPIError("findCards", "Invalid query")

        def get_card(self, card_id: int) -> dict[str, Any]:
            raise AssertionError("get_card should not be called on invalid query")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(search_cmd, ["--query", "bad("], obj=_base_obj(backend="ankiconnect"))
    payload = _error_payload(result)

    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert payload["error"]["details"]["query"] == "bad("