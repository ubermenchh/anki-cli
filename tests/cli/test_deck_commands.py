from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import pytest
from click.testing import CliRunner

import anki_cli.cli.commands.deck as deck_cmd_mod
from anki_cli.backends.factory import BackendFactoryError
from anki_cli.cli.commands.deck import (
    deck_cmd,
    deck_config_cmd,
    deck_config_set_cmd,
    deck_create_cmd,
    deck_delete_cmd,
    deck_rename_cmd,
    decks_cmd,
)
from anki_cli.cli.dispatcher import get_command


def _base_obj(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "format": "json",
        "backend": "direct",
        "collection_path": None,
        "no_color": True,
        "copy": False,
        "yes": False,
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

    monkeypatch.setattr(deck_cmd_mod, "backend_session_from_context", fake_session)


def test_decks_cmd_json_mode_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def get_decks(self) -> list[dict[str, Any]]:
            return [
                {"id": 1, "name": "Root"},
                {"id": 2, "name": "A\x1fB"},
            ]

        def get_due_counts(self, deck: str | None = None) -> dict[str, int]:
            if deck == "Root":
                return {"new": 1, "learn": 2, "review": 3, "total": 6}
            return {"new": 4, "learn": 5, "review": 6, "total": 15}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(decks_cmd, [], obj=_base_obj(format="json"))
    payload = _success_payload(result)

    assert payload["meta"]["command"] == "decks"
    assert payload["data"]["count"] == 2
    assert payload["data"]["items"] == [
        {
            "id": 1,
            "name": "Root",
            "new": 1,
            "learn": 2,
            "review": 3,
            "total_due": 6,
            "level": 0,
        },
        {
            "id": 2,
            "name": "A::B",
            "new": 4,
            "learn": 5,
            "review": 6,
            "total_due": 15,
            "level": 1,
        },
    ]


def test_decks_cmd_table_mode_builds_indented_names(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def get_decks(self) -> list[dict[str, Any]]:
            return [
                {"id": 1, "name": "Root"},
                {"id": 2, "name": "A::B"},
            ]

        def get_due_counts(self, deck: str | None = None) -> dict[str, int]:
            if deck == "Root":
                return {"new": 1, "learn": 0, "review": 0, "total": 1}
            return {"new": 0, "learn": 2, "review": 3, "total": 5}

    class CaptureFormatter:
        def __init__(self) -> None:
            self.command: str | None = None
            self.data: Any = None

        def emit_success(self, *, command: str, data: Any) -> None:
            self.command = command
            self.data = data

        def emit_error(
            self,
            *,
            command: str,
            code: str,
            message: str,
            details: dict[str, Any] | None = None,
        ) -> None:
            raise AssertionError(f"unexpected error: {command} {code} {message} {details}")

    cap = CaptureFormatter()
    _patch_session(monkeypatch, Backend())
    monkeypatch.setattr(deck_cmd_mod, "formatter_from_ctx", lambda ctx: cap)

    runner = CliRunner()
    result = runner.invoke(decks_cmd, [], obj=_base_obj(format="table"))

    assert result.exit_code == 0
    assert cap.command == "decks"
    assert cap.data == {
        "count": 2,
        "items": [
            {"name": "Root", "new": 1, "learn": 0, "review": 0, "total": 1},
            {"name": "  B", "new": 0, "learn": 2, "review": 3, "total": 5},
        ],
    }


def test_decks_cmd_backend_unavailable_exit_7(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_session(obj: dict[str, Any]):
        raise BackendFactoryError("backend down")

    monkeypatch.setattr(deck_cmd_mod, "backend_session_from_context", failing_session)

    runner = CliRunner()
    result = runner.invoke(decks_cmd, [], obj=_base_obj(backend="direct"))

    payload = _error_payload(result)
    assert result.exit_code == 7
    assert payload["error"]["code"] == "BACKEND_UNAVAILABLE"
    assert payload["error"]["details"] == {"backend": "direct"}


def test_deck_cmd_success_trims_name(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class Backend:
        def get_deck(self, name: str) -> dict[str, Any]:
            captured["name"] = name
            return {"name": name, "id": 1}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(deck_cmd, ["--deck", "  Root  "], obj=_base_obj())

    payload = _success_payload(result)
    assert payload["meta"]["command"] == "deck"
    assert payload["data"] == {"name": "Root", "id": 1}
    assert captured["name"] == "Root"


def test_deck_cmd_not_found_exit_4(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def get_deck(self, name: str) -> dict[str, Any]:
            raise LookupError("missing")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(deck_cmd, ["--deck", "Root"], obj=_base_obj())

    payload = _error_payload(result)
    assert result.exit_code == 4
    assert payload["error"]["code"] == "ENTITY_NOT_FOUND"
    assert payload["error"]["details"] == {"deck": "Root"}


def test_deck_create_invalid_name_exit_2() -> None:
    runner = CliRunner()
    result = runner.invoke(deck_create_cmd, ["--name", "   "], obj=_base_obj())

    payload = _error_payload(result)
    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert "Deck name cannot be empty" in payload["error"]["message"]


def test_deck_create_invalid_hierarchy_exit_2() -> None:
    runner = CliRunner()
    result = runner.invoke(deck_create_cmd, ["--name", "A::"], obj=_base_obj())

    payload = _error_payload(result)
    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert "empty segment" in payload["error"]["message"]


def test_deck_create_success_with_created_and_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class Backend:
        def create_deck(self, *, name: str) -> dict[str, Any]:
            calls.append(name)
            if name == "A":
                return {"name": "A", "created": False}
            return {"name": name, "created": True}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(deck_create_cmd, ["--name", "A::B"], obj=_base_obj())

    payload = _success_payload(result)
    assert payload["meta"]["command"] == "deck:create"
    assert payload["data"] == {
        "requested": "A::B",
        "chain": ["A", "A::B"],
        "created_count": 1,
        "existing_count": 1,
        "created": [{"name": "A::B", "created": True}],
        "existing": [{"name": "A", "created": False}],
    }
    assert calls == ["A", "A::B"]


def test_deck_rename_invalid_target_exit_2() -> None:
    runner = CliRunner()
    result = runner.invoke(
        deck_rename_cmd,
        ["--from", "A", "--to", "A::"],
        obj=_base_obj(),
    )

    payload = _error_payload(result)
    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"


def test_deck_rename_not_found_exit_4(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def rename_deck(self, *, old_name: str, new_name: str) -> dict[str, Any]:
            raise LookupError("missing")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(
        deck_rename_cmd,
        ["--from", "A", "--to", "B"],
        obj=_base_obj(),
    )

    payload = _error_payload(result)
    assert result.exit_code == 4
    assert payload["error"]["code"] == "ENTITY_NOT_FOUND"
    assert payload["error"]["details"] == {"from": "A", "to": "B"}


def test_deck_rename_backend_value_error_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def rename_deck(self, *, old_name: str, new_name: str) -> dict[str, Any]:
            raise ValueError("bad rename")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(
        deck_rename_cmd,
        ["--from", "A", "--to", "B"],
        obj=_base_obj(),
    )

    payload = _error_payload(result)
    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert payload["error"]["details"] == {"from": "A", "to": "B"}


def test_deck_delete_requires_yes_exit_2() -> None:
    runner = CliRunner()
    result = runner.invoke(deck_delete_cmd, ["--deck", "Root"], obj=_base_obj(yes=False))

    payload = _error_payload(result)
    assert result.exit_code == 2
    assert payload["error"]["code"] == "CONFIRMATION_REQUIRED"


def test_deck_delete_success_when_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class Backend:
        def delete_deck(self, *, name: str) -> dict[str, Any]:
            captured["name"] = name
            return {"deck": name, "deleted": True}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(deck_delete_cmd, ["--deck", "  Root  "], obj=_base_obj(yes=True))

    payload = _success_payload(result)
    assert payload["meta"]["command"] == "deck:delete"
    assert payload["data"] == {"deck": "Root", "deleted": True}
    assert captured["name"] == "Root"


def test_deck_config_lookup_error_exit_4(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def get_deck_config(self, name: str) -> dict[str, Any]:
            raise LookupError("missing")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(deck_config_cmd, ["--deck", "Root"], obj=_base_obj())

    payload = _error_payload(result)
    assert result.exit_code == 4
    assert payload["error"]["code"] == "ENTITY_NOT_FOUND"
    assert payload["error"]["details"] == {"deck": "Root"}


def test_deck_config_set_requires_updates_exit_2() -> None:
    runner = CliRunner()
    result = runner.invoke(deck_config_set_cmd, ["--deck", "Root"], obj=_base_obj())

    payload = _error_payload(result)
    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert "at least one update option" in payload["error"]["message"]


def test_deck_config_set_invalid_steps_exit_2() -> None:
    runner = CliRunner()
    result = runner.invoke(
        deck_config_set_cmd,
        ["--deck", "Root", "--learn-steps", "1,nope"],
        obj=_base_obj(),
    )

    payload = _error_payload(result)
    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert "Failed to parse step values" in payload["error"]["message"]


def test_deck_config_set_success(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class Backend:
        def set_deck_config(self, name: str, updates: dict[str, Any]) -> dict[str, Any]:
            captured["name"] = name
            captured["updates"] = updates
            return {"deck": name, "updated": True, "config": updates}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(
        deck_config_set_cmd,
        [
            "--deck",
            "  Root  ",
            "--new-per-day",
            "20",
            "--reviews-per-day",
            "100",
            "--desired-retention",
            "0.9",
            "--learn-steps",
            "1,10",
            "--relearn-steps",
            "5",
        ],
        obj=_base_obj(),
    )

    payload = _success_payload(result)
    assert payload["meta"]["command"] == "deck:config:set"
    assert payload["data"]["deck"] == "Root"

    assert captured["name"] == "Root"
    assert captured["updates"] == {
        "new_per_day": 20,
        "reviews_per_day": 100,
        "desired_retention": 0.9,
        "learn_steps": [1.0, 10.0],
        "relearn_steps": [5.0],
    }


def test_deck_config_set_backend_failure_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def set_deck_config(self, name: str, updates: dict[str, Any]) -> dict[str, Any]:
            raise ValueError("cannot update")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(
        deck_config_set_cmd,
        ["--deck", "Root", "--new-per-day", "20"],
        obj=_base_obj(),
    )

    payload = _error_payload(result)
    assert result.exit_code == 1
    assert payload["error"]["code"] == "BACKEND_OPERATION_FAILED"
    assert payload["error"]["details"] == {"deck": "Root", "updates": {"new_per_day": 20}}


def test_deck_commands_are_registered() -> None:
    assert get_command("decks") is not None
    assert get_command("deck") is not None
    assert get_command("deck:create") is not None
    assert get_command("deck:rename") is not None
    assert get_command("deck:delete") is not None
    assert get_command("deck:config") is not None
    assert get_command("deck:config:set") is not None