from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import pytest
from click.testing import CliRunner

import anki_cli.cli.commands.tag as tag_cmd_mod
from anki_cli.backends.factory import BackendFactoryError
from anki_cli.cli.commands.tag import (
    tag_add_cmd,
    tag_cmd,
    tag_remove_cmd,
    tag_rename_cmd,
    tags_cmd,
)
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


def _patch_session(monkeypatch: pytest.MonkeyPatch, backend: Any) -> None:
    @contextmanager
    def fake_session(obj: dict[str, Any]):
        yield backend

    monkeypatch.setattr(tag_cmd_mod, "backend_session_from_context", fake_session)


def test_tags_cmd_prefers_tag_counts_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def get_tag_counts(self) -> list[dict[str, Any]]:
            return [{"tag": "b", "count": 2}, {"tag": "a", "count": 1}]

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(tags_cmd, [], obj=_base_obj())
    payload = _success_payload(result)

    assert payload["meta"]["command"] == "tags"
    assert payload["data"] == {
        "count": 2,
        "items": [{"tag": "a", "count": 1}, {"tag": "b", "count": 2}],
    }


def test_tags_cmd_falls_back_to_get_tags_when_counts_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def get_tag_counts(self) -> list[dict[str, Any]]:
            raise RuntimeError("no counts")

        def get_tags(self) -> list[str]:
            return ["beta", "alpha"]

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(tags_cmd, [], obj=_base_obj())
    payload = _success_payload(result)

    assert payload["meta"]["command"] == "tags"
    assert payload["data"] == {"count": 2, "items": ["alpha", "beta"]}


def test_tags_cmd_backend_unavailable_exit_7(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_session(obj: dict[str, Any]):
        raise BackendFactoryError("backend down")

    monkeypatch.setattr(tag_cmd_mod, "backend_session_from_context", failing_session)

    runner = CliRunner()
    result = runner.invoke(tags_cmd, [], obj=_base_obj(backend="direct"))

    payload = _error_payload(result)
    assert result.exit_code == 7
    assert payload["error"]["code"] == "BACKEND_UNAVAILABLE"
    assert payload["error"]["details"] == {"backend": "direct"}


def test_tag_cmd_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def find_notes(self, query: str) -> list[int]:
            assert query == 'tag:"foo"'
            return [10, 11]

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(tag_cmd, ["--tag", "foo"], obj=_base_obj())
    payload = _success_payload(result)

    assert payload["meta"]["command"] == "tag"
    assert payload["data"] == {"tag": "foo", "count": 2, "note_ids": [10, 11]}


def test_tag_add_requires_id_or_query_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def add_tags(self, note_ids: list[int], tags: list[str]) -> dict[str, Any]:
            raise AssertionError("should not be called")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(tag_add_cmd, ["--tag", "x"], obj=_base_obj())

    payload = _error_payload(result)
    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert "Provide --id or --query" in payload["error"]["message"]


def test_tag_add_requires_non_empty_tag_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def add_tags(self, note_ids: list[int], tags: list[str]) -> dict[str, Any]:
            raise AssertionError("should not be called")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(tag_add_cmd, ["--id", "1", "--tag", "   "], obj=_base_obj())

    payload = _error_payload(result)
    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert "Tag value cannot be empty" in payload["error"]["message"]


def test_tag_add_success_with_id(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class Backend:
        def add_tags(self, note_ids: list[int], tags: list[str]) -> dict[str, Any]:
            captured["note_ids"] = note_ids
            captured["tags"] = tags
            return {"updated": 1, "note_ids": note_ids, "tags": tags}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(tag_add_cmd, ["--id", "5", "--tag", " b, a b "], obj=_base_obj())
    payload = _success_payload(result)

    assert payload["meta"]["command"] == "tag:add"
    assert payload["data"] == {"updated": 1, "note_ids": [5], "tags": ["b", "a", "b"]}
    assert captured == {"note_ids": [5], "tags": ["b", "a", "b"]}


def test_tag_add_success_with_query(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class Backend:
        def find_notes(self, query: str) -> list[int]:
            assert query == "deck:Default"
            return [1, 2]

        def add_tags(self, note_ids: list[int], tags: list[str]) -> dict[str, Any]:
            captured["note_ids"] = note_ids
            captured["tags"] = tags
            return {"updated": 2, "note_ids": note_ids, "tags": tags}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(
        tag_add_cmd,
        ["--query", "deck:Default", "--tag", "foo"],
        obj=_base_obj(),
    )
    payload = _success_payload(result)

    assert payload["data"]["updated"] == 2
    assert captured == {"note_ids": [1, 2], "tags": ["foo"]}


def test_tag_remove_requires_id_or_query_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def remove_tags(self, note_ids: list[int], tags: list[str]) -> dict[str, Any]:
            raise AssertionError("should not be called")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(tag_remove_cmd, ["--tag", "x"], obj=_base_obj())

    payload = _error_payload(result)
    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"


def test_tag_remove_success(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class Backend:
        def remove_tags(self, note_ids: list[int], tags: list[str]) -> dict[str, Any]:
            captured["note_ids"] = note_ids
            captured["tags"] = tags
            return {"updated": 1, "note_ids": note_ids, "tags": tags}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(tag_remove_cmd, ["--id", "9", "--tag", "x y"], obj=_base_obj())
    payload = _success_payload(result)

    assert payload["meta"]["command"] == "tag:remove"
    assert payload["data"] == {"updated": 1, "note_ids": [9], "tags": ["x", "y"]}
    assert captured == {"note_ids": [9], "tags": ["x", "y"]}


def test_tag_rename_success(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class Backend:
        def rename_tag(self, old_tag: str, new_tag: str) -> dict[str, Any]:
            captured["old"] = old_tag
            captured["new"] = new_tag
            return {"from": old_tag, "to": new_tag, "updated": 3}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(
        tag_rename_cmd,
        ["--from", " old ", "--to", " new "],
        obj=_base_obj(),
    )
    payload = _success_payload(result)

    assert payload["meta"]["command"] == "tag:rename"
    assert payload["data"] == {"from": "old", "to": "new", "updated": 3}
    assert captured == {"old": "old", "new": "new"}


def test_tag_rename_backend_value_error_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def rename_tag(self, old_tag: str, new_tag: str) -> dict[str, Any]:
            raise ValueError("bad rename")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(
        tag_rename_cmd,
        ["--from", "a", "--to", "b"],
        obj=_base_obj(),
    )

    payload = _error_payload(result)
    assert result.exit_code == 1
    assert payload["error"]["code"] == "BACKEND_OPERATION_FAILED"
    assert payload["error"]["details"] == {"from": "a", "to": "b"}


def test_tag_commands_are_registered() -> None:
    assert get_command("tags") is not None
    assert get_command("tag") is not None
    assert get_command("tag:add") is not None
    assert get_command("tag:remove") is not None
    assert get_command("tag:rename") is not None