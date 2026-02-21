from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import pytest
from click.testing import CliRunner

import anki_cli.cli.commands.note as note_cmd_mod
from anki_cli.backends.factory import BackendFactoryError
from anki_cli.cli.commands.note import (
    note_add_cmd,
    note_bulk_cmd,
    note_cmd,
    note_delete_cmd,
    note_edit_cmd,
    note_fields_cmd,
    notes_cmd,
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

    monkeypatch.setattr(note_cmd_mod, "backend_session_from_context", fake_session)


def test_notes_cmd_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def find_notes(self, query: str) -> list[int]:
            assert query == "tag:foo"
            return [11, 12]

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(notes_cmd, ["--query", "tag:foo"], obj=_base_obj())

    payload = _success_payload(result)
    assert payload["meta"]["command"] == "notes"
    assert payload["data"] == {"query": "tag:foo", "count": 2, "ids": [11, 12]}


def test_notes_cmd_backend_unavailable_exit_7(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_session(obj: dict[str, Any]):
        raise BackendFactoryError("backend down")

    monkeypatch.setattr(note_cmd_mod, "backend_session_from_context", failing_session)

    runner = CliRunner()
    result = runner.invoke(notes_cmd, ["--query", ""], obj=_base_obj(backend="direct"))

    payload = _error_payload(result)
    assert result.exit_code == 7
    assert payload["error"]["code"] == "BACKEND_UNAVAILABLE"
    assert "backend down" in payload["error"]["message"]
    assert payload["error"]["details"] == {"backend": "direct"}


def test_note_cmd_not_found_exit_4(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def get_note(self, note_id: int) -> dict[str, Any]:
            raise LookupError("not found")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(note_cmd, ["--id", "42"], obj=_base_obj())

    payload = _error_payload(result)
    assert result.exit_code == 4
    assert payload["error"]["code"] == "ENTITY_NOT_FOUND"
    assert payload["error"]["details"] == {"id": 42}


def test_note_add_requires_fields_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def add_note(self, **kwargs: Any) -> int:
            raise AssertionError("should not be called")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(
        note_add_cmd,
        ["--deck", "Default", "--notetype", "Basic"],
        obj=_base_obj(),
    )

    payload = _error_payload(result)
    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert "No fields provided" in payload["error"]["message"]


def test_note_add_invalid_dynamic_fields_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def add_note(self, **kwargs: Any) -> int:
            raise AssertionError("should not be called")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(
        note_add_cmd,
        ["--deck", "Default", "--notetype", "Basic", "Front", "Q"],
        obj=_base_obj(),
    )

    payload = _error_payload(result)
    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert "Unexpected field token" in payload["error"]["message"]


def test_note_add_success_calls_backend_and_returns_payload(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    class Backend:
        def add_note(
            self,
            *,
            deck: str,
            notetype: str,
            fields: dict[str, str],
            tags: list[str] | None = None,
            allow_duplicate: bool = False,
        ) -> int:
            captured["deck"] = deck
            captured["notetype"] = notetype
            captured["fields"] = fields
            captured["tags"] = tags
            captured["allow_duplicate"] = allow_duplicate
            return 999

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(
        note_add_cmd,
        [
            "--deck",
            "  Default  ",
            "--notetype",
            "  Basic  ",
            "--tags",
            "b, a b",
            "--allow-duplicate",
            "--Front",
            "Q",
            "--Back",
            "A",
        ],
        obj=_base_obj(),
    )

    payload = _success_payload(result)
    assert payload["meta"]["command"] == "note:add"
    assert payload["data"] == {
        "id": 999,
        "deck": "  Default  ",
        "notetype": "  Basic  ",
        "fields": {"Front": "Q", "Back": "A"},
        "tags": ["a", "b"],
    }

    assert captured == {
        "deck": "Default",
        "notetype": "Basic",
        "fields": {"Front": "Q", "Back": "A"},
        "tags": ["a", "b"],
        "allow_duplicate": True,
    }


def test_note_edit_requires_fields_or_tags_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def update_note(self, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("should not be called")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(note_edit_cmd, ["--id", "10"], obj=_base_obj())

    payload = _error_payload(result)
    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert "Nothing to update" in payload["error"]["message"]


def test_note_edit_success(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class Backend:
        def update_note(
            self,
            *,
            note_id: int,
            fields: dict[str, str] | None = None,
            tags: list[str] | None = None,
        ) -> dict[str, Any]:
            captured["note_id"] = note_id
            captured["fields"] = fields
            captured["tags"] = tags
            return {"note_id": note_id, "updated_fields": True, "updated_tags": True}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(
        note_edit_cmd,
        ["--id", "9", "--tags", "b,a b", "--Front", "Updated"],
        obj=_base_obj(),
    )

    payload = _success_payload(result)
    assert payload["meta"]["command"] == "note:edit"
    assert payload["data"]["note_id"] == 9

    assert captured == {
        "note_id": 9,
        "fields": {"Front": "Updated"},
        "tags": ["a", "b"],
    }


def test_note_delete_requires_yes_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def delete_notes(self, note_ids: list[int]) -> dict[str, Any]:
            raise AssertionError("should not be called")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(note_delete_cmd, ["--id", "21"], obj=_base_obj(yes=False))

    payload = _error_payload(result)
    assert result.exit_code == 2
    assert payload["error"]["code"] == "CONFIRMATION_REQUIRED"


def test_note_delete_success_when_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class Backend:
        def delete_notes(self, note_ids: list[int]) -> dict[str, Any]:
            captured["note_ids"] = note_ids
            return {"deleted": 1, "note_ids": note_ids}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(note_delete_cmd, ["--id", "21"], obj=_base_obj(yes=True))

    payload = _success_payload(result)
    assert payload["meta"]["command"] == "note:delete"
    assert payload["data"] == {"deleted": 1, "note_ids": [21]}
    assert captured["note_ids"] == [21]


def test_note_bulk_invalid_json_from_stdin_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def add_notes(self, notes: list[dict[str, Any]]) -> list[int | None]:
            raise AssertionError("should not be called")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(
        note_bulk_cmd,
        ["--deck", "Default", "--notetype", "Basic"],
        input="not-json",
        obj=_base_obj(),
    )

    payload = _error_payload(result)
    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert "Failed to read JSON input" in payload["error"]["message"]


def test_note_bulk_requires_array_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def add_notes(self, notes: list[dict[str, Any]]) -> list[int | None]:
            raise AssertionError("should not be called")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(
        note_bulk_cmd,
        ["--deck", "Default", "--notetype", "Basic"],
        input='{"fields":{"Front":"Q"}}',
        obj=_base_obj(),
    )

    payload = _error_payload(result)
    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert "must be a JSON array" in payload["error"]["message"]


def test_note_bulk_success(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class Backend:
        def add_notes(self, notes: list[dict[str, Any]]) -> list[int | None]:
            captured["notes"] = notes
            return [1001, None, 1003]

    _patch_session(monkeypatch, Backend())

    payload_in = json.dumps(
        [
            {"fields": {"Front": "Q1", "Back": "A1"}, "tags": ["x", "y"]},
            {"fields": {"Front": "Q2"}, "tags": "tag1,tag2"},
            {"fields": {"Front": "Q3"}},
        ]
    )

    runner = CliRunner()
    result = runner.invoke(
        note_bulk_cmd,
        ["--deck", "Default", "--notetype", "Basic"],
        input=payload_in,
        obj=_base_obj(),
    )

    payload = _success_payload(result)
    assert payload["meta"]["command"] == "note:bulk"
    assert payload["data"] == {
        "count": 3,
        "created": 2,
        "failed": 1,
        "ids": [1001, None, 1003],
    }

    assert captured["notes"] == [
        {
            "deck": "Default",
            "notetype": "Basic",
            "fields": {"Front": "Q1", "Back": "A1"},
            "tags": ["x", "y"],
        },
        {
            "deck": "Default",
            "notetype": "Basic",
            "fields": {"Front": "Q2"},
            "tags": "tag1,tag2",
        },
        {
            "deck": "Default",
            "notetype": "Basic",
            "fields": {"Front": "Q3"},
            "tags": [],
        },
    ]


def test_note_fields_success_parses_selected_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class Backend:
        def get_note_fields(
            self, 
            *, 
            note_id: int, 
            fields: list[str] | None = None
        ) -> dict[str, str]:
            captured["note_id"] = note_id
            captured["fields"] = fields
            return {"Front": "Q"}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(
        note_fields_cmd,
        ["--id", "7", "--fields", "Front, Back ,"],
        obj=_base_obj(),
    )

    payload = _success_payload(result)
    assert payload["meta"]["command"] == "note:fields"
    assert payload["data"] == {"id": 7, "fields": {"Front": "Q"}}
    assert captured == {"note_id": 7, "fields": ["Front", "Back"]}


def test_note_fields_backend_failure_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def get_note_fields(
            self, 
            *, 
            note_id: int, 
            fields: list[str] | None = None
        ) -> dict[str, str]:
            raise LookupError("missing")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(
        note_fields_cmd,
        ["--id", "7", "--fields", "Front"],
        obj=_base_obj(),
    )

    payload = _error_payload(result)
    assert result.exit_code == 1
    assert payload["error"]["code"] == "BACKEND_OPERATION_FAILED"
    assert payload["error"]["details"] == {"id": 7, "fields": ["Front"]}


def test_note_commands_are_registered() -> None:
    assert get_command("notes") is not None
    assert get_command("note") is not None
    assert get_command("note:add") is not None
    assert get_command("note:edit") is not None
    assert get_command("note:delete") is not None
    assert get_command("note:bulk") is not None
    assert get_command("note:fields") is not None