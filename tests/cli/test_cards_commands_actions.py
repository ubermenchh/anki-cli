from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import pytest
from click.testing import CliRunner

import anki_cli.cli.commands.cards as cards_cmd_mod
from anki_cli.backends.ankiconnect import AnkiConnectAPIError
from anki_cli.backends.factory import BackendFactoryError
from anki_cli.cli.commands.cards import (
    card_bury_cmd,
    card_flag_cmd,
    card_move_cmd,
    card_reschedule_cmd,
    card_reset_cmd,
    card_suspend_cmd,
    card_unbury_cmd,
    card_unsuspend_cmd,
    cards_ids_cmd,
)
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


def _patch_session(monkeypatch: pytest.MonkeyPatch, backend: Any) -> None:
    @contextmanager
    def fake_session(obj: dict[str, Any]):
        yield backend

    monkeypatch.setattr(cards_cmd_mod, "backend_session_from_context", fake_session)


def test_cards_ids_cmd_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def find_cards(self, query: str) -> list[int]:
            assert query == "deck:Default"
            return [1, 2]

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(cards_ids_cmd, ["--query", "deck:Default"], obj=_base_obj())
    payload = _success_payload(result)

    assert payload["meta"]["command"] == "cards:ids"
    assert payload["data"] == {"query": "deck:Default", "count": 2, "ids": [1, 2]}


def test_cards_ids_cmd_backend_unavailable_exit_7(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_session(obj: dict[str, Any]):
        raise BackendFactoryError("backend down")

    monkeypatch.setattr(cards_cmd_mod, "backend_session_from_context", failing_session)

    runner = CliRunner()
    result = runner.invoke(cards_ids_cmd, ["--query", ""], obj=_base_obj(backend="direct"))

    payload = _error_payload(result)
    assert result.exit_code == 7
    assert payload["error"]["code"] == "BACKEND_UNAVAILABLE"
    assert payload["error"]["details"] == {"backend": "direct"}


def _assert_requires_id_or_query(
    runner: CliRunner,
    command,
    args: list[str],
) -> None:
    result = runner.invoke(command, args, obj=_base_obj())
    payload = _error_payload(result)
    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert "Provide --id or --query." in payload["error"]["message"]


def test_card_suspend_requires_id_or_query() -> None:
    _assert_requires_id_or_query(CliRunner(), card_suspend_cmd, [])


def test_card_unsuspend_requires_id_or_query() -> None:
    _assert_requires_id_or_query(CliRunner(), card_unsuspend_cmd, [])


def test_card_move_requires_id_or_query() -> None:
    _assert_requires_id_or_query(CliRunner(), card_move_cmd, ["--deck", "Target"])


def test_card_flag_requires_id_or_query() -> None:
    _assert_requires_id_or_query(CliRunner(), card_flag_cmd, ["--flag", "2"])


def test_card_bury_requires_id_or_query() -> None:
    _assert_requires_id_or_query(CliRunner(), card_bury_cmd, [])


def test_card_reschedule_requires_id_or_query() -> None:
    _assert_requires_id_or_query(CliRunner(), card_reschedule_cmd, ["--days", "3"])


def test_card_reset_requires_id_or_query() -> None:
    _assert_requires_id_or_query(CliRunner(), card_reset_cmd, [])


@pytest.mark.parametrize(
    ("command", "method_name", "args", "expected_result"),
    [
        (
            card_suspend_cmd,
            "suspend_cards",
            ["--id", "9"],
            {"suspended": 1, "card_ids": [9]},
        ),
        (
            card_unsuspend_cmd,
            "unsuspend_cards",
            ["--id", "10"],
            {"unsuspended": 1, "card_ids": [10]},
        ),
        (
            card_move_cmd,
            "move_cards",
            ["--id", "11", "--deck", "Target"],
            {"moved": 1, "card_ids": [11], "deck": "Target"},
        ),
        (
            card_flag_cmd,
            "set_card_flag",
            ["--id", "12", "--flag", "3"],
            {"updated": 1, "card_ids": [12], "flag": 3},
        ),
        (
            card_bury_cmd,
            "bury_cards",
            ["--id", "13"],
            {"buried": 1, "card_ids": [13]},
        ),
        (
            card_reschedule_cmd,
            "reschedule_cards",
            ["--id", "14", "--days", "5"],
            {"rescheduled": 1, "card_ids": [14], "days": 5},
        ),
        (
            card_reset_cmd,
            "reset_cards",
            ["--id", "15"],
            {"reset": 1, "card_ids": [15]},
        ),
    ],
)
def test_action_commands_success_with_id(
    monkeypatch: pytest.MonkeyPatch,
    command,
    method_name: str,
    args: list[str],
    expected_result: dict[str, Any],
) -> None:
    calls: dict[str, Any] = {}

    class Backend:
        def find_cards(self, query: str) -> list[int]:
            raise AssertionError("find_cards should not be called when --id is provided")

        def suspend_cards(self, ids: list[int]) -> dict[str, Any]:
            calls["ids"] = ids
            return {"suspended": len(ids), "card_ids": ids}

        def unsuspend_cards(self, ids: list[int]) -> dict[str, Any]:
            calls["ids"] = ids
            return {"unsuspended": len(ids), "card_ids": ids}

        def move_cards(self, ids: list[int], deck: str) -> dict[str, Any]:
            calls["ids"] = ids
            calls["deck"] = deck
            return {"moved": len(ids), "card_ids": ids, "deck": deck}

        def set_card_flag(self, ids: list[int], flag: int) -> dict[str, Any]:
            calls["ids"] = ids
            calls["flag"] = flag
            return {"updated": len(ids), "card_ids": ids, "flag": flag}

        def bury_cards(self, ids: list[int]) -> dict[str, Any]:
            calls["ids"] = ids
            return {"buried": len(ids), "card_ids": ids}

        def reschedule_cards(self, ids: list[int], days: int) -> dict[str, Any]:
            calls["ids"] = ids
            calls["days"] = days
            return {"rescheduled": len(ids), "card_ids": ids, "days": days}

        def reset_cards(self, ids: list[int]) -> dict[str, Any]:
            calls["ids"] = ids
            return {"reset": len(ids), "card_ids": ids}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(command, args, obj=_base_obj())
    payload = _success_payload(result)

    assert payload["data"] == expected_result
    assert calls["ids"] == [int(args[1])]


def test_action_command_uses_query_when_no_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    class Backend:
        def find_cards(self, query: str) -> list[int]:
            calls["query"] = query
            return [101, 102]

        def suspend_cards(self, ids: list[int]) -> dict[str, Any]:
            calls["ids"] = ids
            return {"suspended": len(ids), "card_ids": ids}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(
        card_suspend_cmd,
        ["--query", "deck:Default"],
        obj=_base_obj(),
    )
    payload = _success_payload(result)

    assert payload["data"] == {"suspended": 2, "card_ids": [101, 102]}
    assert calls == {"query": "deck:Default", "ids": [101, 102]}


@pytest.mark.parametrize(
    "command,args",
    [
        (card_suspend_cmd, ["--id", "1"]),
        (card_unsuspend_cmd, ["--id", "1"]),
        (card_move_cmd, ["--id", "1", "--deck", "X"]),
        (card_flag_cmd, ["--id", "1", "--flag", "2"]),
        (card_bury_cmd, ["--id", "1"]),
        (card_unbury_cmd, []),
        (card_reschedule_cmd, ["--id", "1", "--days", "2"]),
        (card_reset_cmd, ["--id", "1"]),
    ],
)
def test_action_commands_backend_api_error_exit_1(
    monkeypatch: pytest.MonkeyPatch,
    command,
    args: list[str],
) -> None:
    class Backend:
        def find_cards(self, query: str) -> list[int]:
            return [1]

        def suspend_cards(self, ids: list[int]) -> dict[str, Any]:
            raise AnkiConnectAPIError("suspend", "boom")

        def unsuspend_cards(self, ids: list[int]) -> dict[str, Any]:
            raise AnkiConnectAPIError("unsuspend", "boom")

        def move_cards(self, ids: list[int], deck: str) -> dict[str, Any]:
            raise AnkiConnectAPIError("move", "boom")

        def set_card_flag(self, ids: list[int], flag: int) -> dict[str, Any]:
            raise AnkiConnectAPIError("flag", "boom")

        def bury_cards(self, ids: list[int]) -> dict[str, Any]:
            raise AnkiConnectAPIError("bury", "boom")

        def unbury_cards(self, deck: str | None = None) -> dict[str, Any]:
            raise AnkiConnectAPIError("unbury", "boom")

        def reschedule_cards(self, ids: list[int], days: int) -> dict[str, Any]:
            raise AnkiConnectAPIError("reschedule", "boom")

        def reset_cards(self, ids: list[int]) -> dict[str, Any]:
            raise AnkiConnectAPIError("reset", "boom")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(command, args, obj=_base_obj())
    payload = _error_payload(result)

    assert result.exit_code == 1
    assert payload["error"]["code"] == "BACKEND_OPERATION_FAILED"


def test_card_unbury_success_with_and_without_deck(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str | None] = []

    class Backend:
        def unbury_cards(self, deck: str | None = None) -> dict[str, Any]:
            calls.append(deck)
            if deck is None:
                return {"unburied": True, "scope": "all"}
            return {"unburied": 3, "deck": deck, "card_ids": [1, 2, 3]}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()

    r1 = runner.invoke(card_unbury_cmd, [], obj=_base_obj())
    p1 = _success_payload(r1)
    assert p1["data"] == {"unburied": True, "scope": "all"}

    r2 = runner.invoke(card_unbury_cmd, ["--deck", "  Target  "], obj=_base_obj())
    p2 = _success_payload(r2)
    assert p2["data"] == {"unburied": 3, "deck": "Target", "card_ids": [1, 2, 3]}

    assert calls == [None, "Target"]


def test_cards_command_registration() -> None:
    assert get_command("cards:ids") is not None
    assert get_command("card:suspend") is not None
    assert get_command("card:unsuspend") is not None
    assert get_command("card:move") is not None
    assert get_command("card:flag") is not None
    assert get_command("card:bury") is not None
    assert get_command("card:unbury") is not None
    assert get_command("card:reschedule") is not None
    assert get_command("card:reset") is not None

def test_cards_ids_cmd_invalid_query_parse_error_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def find_cards(self, query: str) -> list[int]:
            raise SearchParseError("Missing closing ')'", query=query, position=4)

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(cards_ids_cmd, ["--query", "(tag:foo"], obj=_base_obj())
    payload = _error_payload(result)

    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert payload["error"]["details"]["query"] == "(tag:foo"
    assert payload["error"]["details"]["position"] == 4


def test_cards_ids_cmd_invalid_query_ankiconnect_error_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def find_cards(self, query: str) -> list[int]:
            raise AnkiConnectAPIError("findCards", "Invalid search")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(cards_ids_cmd, ["--query", "bad("], obj=_base_obj(backend="ankiconnect"))
    payload = _error_payload(result)

    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert payload["error"]["details"]["query"] == "bad("