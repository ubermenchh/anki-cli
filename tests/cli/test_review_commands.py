from __future__ import annotations

import builtins
import json
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from click.testing import CliRunner

import anki_cli.cli.commands.review as review_cmd_mod
from anki_cli.backends.ankiconnect import AnkiConnectProtocolError
from anki_cli.backends.factory import BackendFactoryError
from anki_cli.cli.commands.review import (
    review_answer_cmd,
    review_cmd,
    review_next_cmd,
    review_preview_cmd,
    review_show_cmd,
    review_start_cmd,
    review_undo_cmd,
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


def _patch_session(monkeypatch, backend: Any) -> None:
    @contextmanager
    def fake_session(obj: dict[str, Any]):
        yield backend

    monkeypatch.setattr(review_cmd_mod, "backend_session_from_context", fake_session)


def test_review_cmd_success_trims_deck_for_backend_call(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class Backend:
        def get_due_counts(self, deck: str | None = None) -> dict[str, int]:
            captured["deck"] = deck
            return {"new": 1, "learn": 2, "review": 3, "total": 6}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(review_cmd, ["--deck", "  Default  "], obj=_base_obj())
    payload = _success_payload(result)

    assert payload["meta"]["command"] == "review"
    assert payload["data"] == {
        "deck": "  Default  ",
        "due_counts": {"new": 1, "learn": 2, "review": 3, "total": 6},
    }
    assert captured["deck"] == "Default"


def test_review_cmd_backend_unavailable_exit_7(monkeypatch) -> None:
    def failing_session(obj: dict[str, Any]):
        raise BackendFactoryError("backend down")

    monkeypatch.setattr(review_cmd_mod, "backend_session_from_context", failing_session)

    runner = CliRunner()
    result = runner.invoke(review_cmd, [], obj=_base_obj(backend="direct"))
    payload = _error_payload(result)

    assert result.exit_code == 7
    assert payload["error"]["code"] == "BACKEND_UNAVAILABLE"
    assert payload["error"]["details"] == {"backend": "direct"}


def test_review_next_uses_direct_store_picker(monkeypatch) -> None:
    calls: dict[str, Any] = {}

    class Store:
        def get_next_due_card(self, deck: str | None = None) -> dict[str, Any]:
            calls["deck"] = deck
            return {"card_id": 77, "kind": "learn_due"}

    class Backend:
        name = "direct"

        def __init__(self) -> None:
            self._store = Store()

    _patch_session(monkeypatch, Backend())
    monkeypatch.setattr(
        review_cmd_mod,
        "_render_card",
        lambda *, backend, card_id, reveal_answer: {
            "rendered": {"question": "Q"},
            "render_error": None,
            "card": {"id": card_id},
        },
    )

    runner = CliRunner()
    result = runner.invoke(review_next_cmd, ["--deck", "  D  "], obj=_base_obj())
    payload = _success_payload(result)

    assert payload["meta"]["command"] == "review:next"
    assert payload["data"] == {
        "deck": "  D  ",
        "kind": "learn_due",
        "card_id": 77,
        "question": "Q",
        "rendered": {"question": "Q"},
    }
    assert calls["deck"] == "D"


def test_review_next_no_card_returns_none_without_render(monkeypatch) -> None:
    class Store:
        def get_next_due_card(self, deck: str | None = None) -> dict[str, Any]:
            return {"card_id": None, "kind": "none"}

    class Backend:
        name = "direct"

        def __init__(self) -> None:
            self._store = Store()

    _patch_session(monkeypatch, Backend())

    def fail_render(**kwargs: Any):
        raise AssertionError("_render_card should not be called")

    monkeypatch.setattr(review_cmd_mod, "_render_card", fail_render)

    runner = CliRunner()
    result = runner.invoke(review_next_cmd, [], obj=_base_obj())
    payload = _success_payload(result)

    assert payload["data"] == {"deck": None, "card_id": None, "kind": "none"}


def test_review_next_falls_back_to_scheduler_for_non_direct(monkeypatch) -> None:
    calls: dict[str, Any] = {}

    class Backend:
        name = "ankiconnect"

    _patch_session(monkeypatch, Backend())

    def fake_pick(backend: Any, *, deck: str | None = None):
        calls["deck"] = deck
        return 55, "review_due"

    monkeypatch.setattr(review_cmd_mod, "pick_next_due_card_id", fake_pick)
    monkeypatch.setattr(
        review_cmd_mod,
        "_render_card",
        lambda *, backend, card_id, reveal_answer: {"rendered": {"question": "QQ"}},
    )

    runner = CliRunner()
    result = runner.invoke(review_next_cmd, ["--deck", "X"], obj=_base_obj())
    payload = _success_payload(result)

    assert payload["data"]["card_id"] == 55
    assert payload["data"]["kind"] == "review_due"
    assert payload["data"]["question"] == "QQ"
    assert calls["deck"] == "X"


def test_review_next_operation_error_exit_1(monkeypatch) -> None:
    class Store:
        def get_next_due_card(self, deck: str | None = None) -> dict[str, Any]:
            return {"card_id": 1, "kind": "learn_due"}

    class Backend:
        name = "direct"

        def __init__(self) -> None:
            self._store = Store()

    _patch_session(monkeypatch, Backend())
    monkeypatch.setattr(
        review_cmd_mod,
        "_render_card",
        lambda **kwargs: (_ for _ in ()).throw(LookupError("render failed")),
    )

    runner = CliRunner()
    result = runner.invoke(review_next_cmd, [], obj=_base_obj())
    payload = _error_payload(result)

    assert result.exit_code == 1
    assert payload["error"]["code"] == "BACKEND_OPERATION_FAILED"
    assert payload["error"]["details"] == {"deck": None}


def test_review_show_no_card(monkeypatch) -> None:
    class Backend:
        name = "direct"

    _patch_session(monkeypatch, Backend())
    monkeypatch.setattr(
        review_cmd_mod, 
        "pick_next_due_card_id", 
        lambda backend, deck=None: (None, "none")
    )

    runner = CliRunner()
    result = runner.invoke(review_show_cmd, [], obj=_base_obj())
    payload = _success_payload(result)

    assert payload["data"] == {"deck": None, "card_id": None, "kind": "none"}


def test_review_show_success(monkeypatch) -> None:
    class Backend:
        name = "direct"

    _patch_session(monkeypatch, Backend())
    monkeypatch.setattr(
        review_cmd_mod, 
        "pick_next_due_card_id", 
        lambda backend, deck=None: (9, "new")
    )
    monkeypatch.setattr(
        review_cmd_mod,
        "_render_card",
        lambda *, backend, card_id, reveal_answer: {"rendered": {"question": "Q", "answer": "A"}},
    )

    runner = CliRunner()
    result = runner.invoke(review_show_cmd, ["--deck", "DeckA"], obj=_base_obj())
    payload = _success_payload(result)

    assert payload["data"] == {
        "deck": "DeckA",
        "kind": "new",
        "card_id": 9,
        "rendered": {"question": "Q", "answer": "A"},
    }


def test_review_preview_direct_success(monkeypatch) -> None:
    class Store:
        def preview_ratings(self, card_id: int) -> list[dict[str, Any]]:
            return [{"ease": 1}, {"ease": 2}]

    class Backend:
        name = "direct"

        def __init__(self) -> None:
            self._store = Store()

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(review_preview_cmd, ["--id", "42"], obj=_base_obj())
    payload = _success_payload(result)

    assert payload["meta"]["command"] == "review:preview"
    assert payload["data"] == {"card_id": 42, "items": [{"ease": 1}, {"ease": 2}]}


def test_review_preview_unsupported_backend_exit_7(monkeypatch) -> None:
    class Backend:
        name = "ankiconnect"

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(review_preview_cmd, ["--id", "42"], obj=_base_obj())
    payload = _error_payload(result)

    assert result.exit_code == 7
    assert payload["error"]["code"] == "BACKEND_UNAVAILABLE"


def test_review_preview_operation_error_exit_1(monkeypatch) -> None:
    class Store:
        def preview_ratings(self, card_id: int) -> list[dict[str, Any]]:
            raise LookupError("missing card")

    class Backend:
        name = "direct"

        def __init__(self) -> None:
            self._store = Store()

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(review_preview_cmd, ["--id", "42"], obj=_base_obj())
    payload = _error_payload(result)

    assert result.exit_code == 1
    assert payload["error"]["code"] == "BACKEND_OPERATION_FAILED"
    assert payload["error"]["details"] == {"id": 42}


def test_review_undo_empty_exit_2(monkeypatch) -> None:
    class Store:
        def restore_card_state(self, snapshot: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("should not be called")

    class Backend:
        name = "direct"
        collection_path = Path("/tmp/col.db")

        def __init__(self) -> None:
            self._store = Store()

    class FakeUndoStore:
        def pop(self, *, collection: str):
            return None

    _patch_session(monkeypatch, Backend())
    monkeypatch.setattr(review_cmd_mod, "UndoStore", FakeUndoStore)

    runner = CliRunner()
    result = runner.invoke(review_undo_cmd, [], obj=_base_obj())
    payload = _error_payload(result)

    assert result.exit_code == 2
    assert payload["error"]["code"] == "UNDO_EMPTY"


def test_review_undo_success(monkeypatch) -> None:
    calls: dict[str, Any] = {}

    class Store:
        def restore_card_state(self, snapshot: dict[str, Any]) -> dict[str, Any]:
            calls["snapshot"] = snapshot
            return {"card_id": 123, "restored": True}

    class Backend:
        name = "direct"
        collection_path = Path("/tmp/col.db")

        def __init__(self) -> None:
            self._store = Store()

    class FakeUndoStore:
        def pop(self, *, collection: str):
            calls["collection"] = collection
            return review_cmd_mod.UndoItem(
                collection=collection,
                card_id=123,
                snapshot={"id": 123, "queue": 2},
                created_at_epoch_ms=1,
            )

    _patch_session(monkeypatch, Backend())
    monkeypatch.setattr(review_cmd_mod, "UndoStore", FakeUndoStore)

    runner = CliRunner()
    result = runner.invoke(review_undo_cmd, [], obj=_base_obj())
    payload = _success_payload(result)

    assert payload["meta"]["command"] == "review:undo"
    assert payload["data"] == {"card_id": 123, "restored": True}
    assert calls["collection"] == "/tmp/col.db"
    assert calls["snapshot"] == {"id": 123, "queue": 2}


def test_review_answer_invalid_rating_exit_2() -> None:
    runner = CliRunner()
    result = runner.invoke(review_answer_cmd, ["--id", "5", "--rating", "bad"], obj=_base_obj())
    payload = _error_payload(result)

    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert payload["error"]["details"] == {"rating": "bad"}


def test_review_answer_success_non_direct(monkeypatch) -> None:
    calls: dict[str, Any] = {}

    class Backend:
        name = "ankiconnect"

        def answer_card(self, *, card_id: int, ease: int) -> dict[str, Any]:
            calls["card_id"] = card_id
            calls["ease"] = ease
            return {"card_id": card_id, "ease": ease, "answered": True}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(review_answer_cmd, ["--id", "7", "--rating", "good"], obj=_base_obj())
    payload = _success_payload(result)

    assert payload["meta"]["command"] == "review:answer"
    assert payload["data"] == {"card_id": 7, "ease": 3, "answered": True}
    assert calls == {"card_id": 7, "ease": 3}


def test_review_answer_direct_pushes_undo_snapshot(monkeypatch) -> None:
    pushed: list[Any] = []
    calls: dict[str, Any] = {}

    class Store:
        def snapshot_card_state(self, card_id: int) -> dict[str, Any]:
            calls["snapshot_card_id"] = card_id
            return {"id": card_id, "queue": 2}

    class Backend:
        name = "direct"
        collection_path = Path("/tmp/col.db")

        def __init__(self) -> None:
            self._store = Store()

        def answer_card(self, *, card_id: int, ease: int) -> dict[str, Any]:
            calls["answer"] = {"card_id": card_id, "ease": ease}
            return {"card_id": card_id, "ease": ease, "answered": True}

    class FakeUndoStore:
        def push(self, item: Any, *, max_items: int = 50) -> None:
            pushed.append(item)

    _patch_session(monkeypatch, Backend())
    monkeypatch.setattr(review_cmd_mod, "UndoStore", FakeUndoStore)
    monkeypatch.setattr(review_cmd_mod, "now_epoch_ms", lambda: 123456)

    runner = CliRunner()
    result = runner.invoke(review_answer_cmd, ["--id", "9", "--rating", "easy"], obj=_base_obj())
    payload = _success_payload(result)

    assert payload["data"] == {"card_id": 9, "ease": 4, "answered": True}
    assert calls["snapshot_card_id"] == 9
    assert calls["answer"] == {"card_id": 9, "ease": 4}

    assert len(pushed) == 1
    item = pushed[0]
    assert item.collection == "/tmp/col.db"
    assert item.card_id == 9
    assert item.snapshot == {"id": 9, "queue": 2}
    assert item.created_at_epoch_ms == 123456


def test_review_answer_operation_error_exit_1(monkeypatch) -> None:
    class Backend:
        name = "ankiconnect"

        def answer_card(self, *, card_id: int, ease: int) -> dict[str, Any]:
            raise LookupError("missing card")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(review_answer_cmd, ["--id", "9", "--rating", "hard"], obj=_base_obj())
    payload = _error_payload(result)

    assert result.exit_code == 1
    assert payload["error"]["code"] == "BACKEND_OPERATION_FAILED"
    assert payload["error"]["details"] == {"id": 9, "rating": "hard", "ease": 2}


def test_review_answer_protocol_error_exit_1(monkeypatch) -> None:
    class Backend:
        name = "ankiconnect"

        def answer_card(self, *, card_id: int, ease: int) -> dict[str, Any]:
            raise AnkiConnectProtocolError("bad protocol")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(review_answer_cmd, ["--id", "9", "--rating", "again"], obj=_base_obj())
    payload = _error_payload(result)

    assert result.exit_code == 1
    assert payload["error"]["code"] == "BACKEND_OPERATION_FAILED"


def test_review_start_tui_not_available_exit_2(monkeypatch) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, globals=None, locals=None, fromlist=(), level=0):
        if name == "anki_cli.tui.review_app":
            raise ImportError("textual missing")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    runner = CliRunner()
    result = runner.invoke(review_start_cmd, [], obj=_base_obj())
    payload = _error_payload(result)

    assert result.exit_code == 2
    assert payload["error"]["code"] == "TUI_NOT_AVAILABLE"
    assert payload["error"]["details"] == {"hint": "Run: uv sync --extra tui"}


def test_review_start_unsupported_backend_exit_2(monkeypatch) -> None:
    module = types.ModuleType("anki_cli.tui.review_app")

    class FakeReviewApp:
        def __init__(self, *, backend: Any, deck: str | None) -> None:
            pass

        def run(self) -> None:
            raise AssertionError("run should not be reached for unsupported backend")

    module.ReviewApp = FakeReviewApp  # type: ignore[assignment]
    monkeypatch.setitem(sys.modules, "anki_cli.tui.review_app", module)

    class Backend:
        name = "ankiconnect"

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(review_start_cmd, ["--deck", "DeckA"], obj=_base_obj())
    payload = _error_payload(result)

    assert result.exit_code == 2
    assert payload["error"]["code"] == "UNSUPPORTED_BACKEND"
    assert payload["error"]["details"] == {"backend": "ankiconnect"}


def test_review_start_success_direct_runs_app(monkeypatch) -> None:
    calls: dict[str, Any] = {}

    module = types.ModuleType("anki_cli.tui.review_app")

    class FakeReviewApp:
        def __init__(self, *, backend: Any, deck: str | None) -> None:
            calls["backend"] = backend
            calls["deck"] = deck

        def run(self) -> None:
            calls["run_called"] = True

    module.ReviewApp = FakeReviewApp  # type: ignore[assignment]
    monkeypatch.setitem(sys.modules, "anki_cli.tui.review_app", module)

    class Backend:
        name = "direct"

    backend = Backend()
    _patch_session(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(review_start_cmd, ["--deck", "  DeckA  "], obj=_base_obj())

    assert result.exit_code == 0, result.output
    assert calls["backend"] is backend
    assert calls["deck"] == "DeckA"
    assert calls["run_called"] is True


def test_review_start_backend_unavailable_exit_7(monkeypatch) -> None:
    module = types.ModuleType("anki_cli.tui.review_app")

    class FakeReviewApp:
        def __init__(self, *, backend: Any, deck: str | None) -> None:
            pass

        def run(self) -> None:
            pass

    module.ReviewApp = FakeReviewApp  # type: ignore[assignment]
    monkeypatch.setitem(sys.modules, "anki_cli.tui.review_app", module)

    def failing_session(obj: dict[str, Any]):
        raise BackendFactoryError("backend down")

    monkeypatch.setattr(review_cmd_mod, "backend_session_from_context", failing_session)

    runner = CliRunner()
    result = runner.invoke(review_start_cmd, [], obj=_base_obj(backend="direct"))
    payload = _error_payload(result)

    assert result.exit_code == 7
    assert payload["error"]["code"] == "BACKEND_UNAVAILABLE"
    assert payload["error"]["details"] == {"backend": "direct"}


def test_review_commands_are_registered() -> None:
    assert get_command("review") is not None
    assert get_command("review:next") is not None
    assert get_command("review:show") is not None
    assert get_command("review:answer") is not None
    assert get_command("review:preview") is not None
    assert get_command("review:undo") is not None
    assert get_command("review:start") is not None