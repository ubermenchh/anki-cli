from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

from click.testing import CliRunner

import anki_cli.cli.commands.cards as cards_cmd_mod
from anki_cli.backends.factory import BackendFactoryError
from anki_cli.cli.commands.cards import card_cmd, card_revlog_cmd
from anki_cli.cli.dispatcher import get_command


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

    monkeypatch.setattr(cards_cmd_mod, "backend_session_from_context", fake_session)


def test_card_cmd_success_without_note_id_and_without_revlog(monkeypatch) -> None:
    class Backend:
        def get_card(self, card_id: int) -> dict[str, Any]:
            return {"id": card_id, "queue": 2}

        def get_revlog(self, card_id: int, limit: int = 50) -> list[dict[str, Any]]:
            raise NotImplementedError

        def get_note_fields(self, note_id: int, fields: list[str] | None = None) -> dict[str, str]:
            raise AssertionError("should not be called when card has no note id")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(card_cmd, ["--id", "10"], obj=_base_obj())
    payload = _success_payload(result)

    assert payload["meta"]["command"] == "card"
    data = payload["data"]
    assert data["id"] == 10
    assert data["queue"] == 2
    assert data["rendered"] is None
    assert "revlog" not in data


def test_card_cmd_success_renders_with_notetype_name_and_bounded_revlog_limit(monkeypatch) -> None:
    calls: dict[str, Any] = {}

    class Backend:
        def get_card(self, card_id: int) -> dict[str, Any]:
            return {"id": card_id, "note": 700, "ord": 0, "notetype_name": "Basic"}

        def get_note_fields(self, note_id: int, fields: list[str] | None = None) -> dict[str, str]:
            assert note_id == 700
            assert fields is None
            return {"Front": "Q", "Back": "A"}

        def get_note(self, note_id: int) -> dict[str, Any]:
            raise AssertionError("should not be called when notetype_name is on card")

        def get_notetype(self, name: str) -> dict[str, Any]:
            assert name == "Basic"
            return {
                "kind": "normal",
                "templates": {
                    "Card 1": {"Front": "{{Front}}", "Back": "{{FrontSide}}/{{Back}}"}
                },
                "styling": {"css": "body{}"},
            }

        def get_revlog(self, card_id: int, limit: int = 50) -> list[dict[str, Any]]:
            calls["revlog"] = {"card_id": card_id, "limit": limit}
            return [{"id": 1}]

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(card_cmd, ["--id", "7", "--revlog-limit", "1500"], obj=_base_obj())
    payload = _success_payload(result)

    data = payload["data"]
    assert data["id"] == 7
    assert data["rendered"] == {
        "notetype": "Basic",
        "ord": 0,
        "question": "Q",
        "answer": "Q/A",
        "css": "body{}",
    }
    assert data["revlog"] == [{"id": 1}]
    assert calls["revlog"] == {"card_id": 7, "limit": 1000}


def test_card_cmd_uses_model_name_from_note_when_notetype_missing_on_card(monkeypatch) -> None:
    class Backend:
        def get_card(self, card_id: int) -> dict[str, Any]:
            return {"id": card_id, "nid": 800, "ord": 0}

        def get_note_fields(self, note_id: int, fields: list[str] | None = None) -> dict[str, str]:
            assert note_id == 800
            return {"Front": "Q", "Back": "A"}

        def get_note(self, note_id: int) -> dict[str, Any]:
            assert note_id == 800
            return {"modelName": "ModelX"}

        def get_notetype(self, name: str) -> dict[str, Any]:
            assert name == "ModelX"
            return {
                "kind": "normal",
                "templates": {"Card 1": {"Front": "{{Front}}", "Back": "{{Back}}"}},
            }

        def get_revlog(self, card_id: int, limit: int = 50) -> list[dict[str, Any]]:
            return []

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(card_cmd, ["--id", "8"], obj=_base_obj())
    payload = _success_payload(result)

    data = payload["data"]
    assert data["rendered"] == {
        "notetype": "ModelX",
        "ord": 0,
        "question": "Q",
        "answer": "A",
        "css": "",
    }
    assert data["revlog"] == []


def test_card_cmd_mid_fallback_with_missing_templates_sets_render_error(monkeypatch) -> None:
    class Backend:
        def get_card(self, card_id: int) -> dict[str, Any]:
            return {"id": card_id, "note_id": 900, "ord": 0}

        def get_note_fields(self, note_id: int, fields: list[str] | None = None) -> dict[str, str]:
            assert note_id == 900
            return {"Front": "Q"}

        def get_note(self, note_id: int) -> dict[str, Any]:
            return {"mid": 123}

        def get_notetypes(self) -> list[dict[str, Any]]:
            return [{"id": 123, "name": "MidModel"}]

        def get_notetype(self, name: str) -> dict[str, Any]:
            assert name == "MidModel"
            return {"kind": "normal", "templates": {}}

        def get_revlog(self, card_id: int, limit: int = 50) -> list[dict[str, Any]]:
            return [{"id": 1}]

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(card_cmd, ["--id", "9"], obj=_base_obj())
    payload = _success_payload(result)

    data = payload["data"]
    assert data["rendered"] is None
    assert "render_error" in data
    assert "No templates found for notetype" in data["render_error"]
    assert "MidModel" in data["render_error"]
    assert data["revlog"] == [{"id": 1}]


def test_card_cmd_entity_not_found_exit_4(monkeypatch) -> None:
    class Backend:
        def get_card(self, card_id: int) -> dict[str, Any]:
            raise LookupError("missing card")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(card_cmd, ["--id", "404"], obj=_base_obj())

    payload = _error_payload(result)
    assert result.exit_code == 4
    assert payload["error"]["code"] == "ENTITY_NOT_FOUND"
    assert payload["error"]["details"] == {"id": 404}


def test_card_cmd_backend_unavailable_exit_7(monkeypatch) -> None:
    def failing_session(obj: dict[str, Any]):
        raise BackendFactoryError("backend down")

    monkeypatch.setattr(cards_cmd_mod, "backend_session_from_context", failing_session)

    runner = CliRunner()
    result = runner.invoke(card_cmd, ["--id", "1"], obj=_base_obj(backend="direct"))

    payload = _error_payload(result)
    assert result.exit_code == 7
    assert payload["error"]["code"] == "BACKEND_UNAVAILABLE"
    assert payload["error"]["details"] == {"backend": "direct"}


def test_card_revlog_cmd_success_with_limit_bounds(monkeypatch) -> None:
    calls: list[dict[str, int]] = []

    class Backend:
        def get_revlog(self, card_id: int, limit: int = 50) -> list[dict[str, Any]]:
            calls.append({"card_id": card_id, "limit": limit})
            return [{"id": 10}, {"id": 11}]

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()

    result_low = runner.invoke(card_revlog_cmd, ["--id", "5", "--limit", "0"], obj=_base_obj())
    payload_low = _success_payload(result_low)
    assert payload_low["data"] == {
        "id": 5, 
        "limit": 1, 
        "count": 2, 
        "items": [{"id": 10}, {"id": 11}]
    }

    result_high = runner.invoke(card_revlog_cmd, ["--id", "5", "--limit", "5000"], obj=_base_obj())
    payload_high = _success_payload(result_high)
    assert payload_high["data"]["limit"] == 1000
    assert payload_high["data"]["count"] == 2

    assert calls == [{"card_id": 5, "limit": 1}, {"card_id": 5, "limit": 1000}]


def test_card_revlog_cmd_entity_not_found_exit_4(monkeypatch) -> None:
    class Backend:
        def get_revlog(self, card_id: int, limit: int = 50) -> list[dict[str, Any]]:
            raise LookupError("missing card")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(card_revlog_cmd, ["--id", "5"], obj=_base_obj())

    payload = _error_payload(result)
    assert result.exit_code == 4
    assert payload["error"]["code"] == "ENTITY_NOT_FOUND"
    assert payload["error"]["details"] == {"id": 5}


def test_card_revlog_cmd_backend_unavailable_exit_7(monkeypatch) -> None:
    def failing_session(obj: dict[str, Any]):
        raise BackendFactoryError("backend down")

    monkeypatch.setattr(cards_cmd_mod, "backend_session_from_context", failing_session)

    runner = CliRunner()
    result = runner.invoke(card_revlog_cmd, ["--id", "5"], obj=_base_obj(backend="direct"))

    payload = _error_payload(result)
    assert result.exit_code == 7
    assert payload["error"]["code"] == "BACKEND_UNAVAILABLE"


def test_card_detail_commands_are_registered() -> None:
    assert get_command("card") is not None
    assert get_command("card:revlog") is not None