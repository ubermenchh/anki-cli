"""The review session shared by the CLI, the review TUI and the REPL (#36)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import anki_cli.core.review_session as session_mod
from anki_cli.core.review_session import (
    IntrospectionUnavailableError,
    NextCard,
    ReviewSession,
    parse_ease,
)
from anki_cli.core.undo import UndoStore


class DirectBackend:
    name = "direct"
    supports_scheduler_introspection = True
    collection_path = Path("/tmp/col.anki2")

    def __init__(self) -> None:
        self.queue = 2
        self.next_pick: dict[str, Any] = {"card_id": 7, "kind": "review_due"}
        self.answers: list[tuple[int, int]] = []
        self.restored: list[dict[str, Any]] = []
        self.picked_decks: list[str | None] = []
        self.fail_answer: Exception | None = None

    def get_next_due_card(self, deck: str | None = None) -> dict[str, Any]:
        self.picked_decks.append(deck)
        return self.next_pick

    def snapshot_card_state(self, card_id: int) -> dict[str, Any]:
        return {"id": card_id, "queue": self.queue}

    def answer_card(self, *, card_id: int, ease: int) -> dict[str, Any]:
        if self.fail_answer is not None:
            raise self.fail_answer
        self.queue = 9  # a snapshot taken after this would see the new state
        self.answers.append((card_id, ease))
        return {"card_id": card_id, "revlog_id": 777}

    def restore_card_state(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        self.restored.append(snapshot)
        return {"restored": True, "card_id": snapshot["id"]}

    def preview_ratings(self, card_id: int) -> list[dict[str, Any]]:
        return [{"ease": n} for n in (1, 2, 3, 4)]


class PlainBackend:
    """No scheduler introspection: AnkiConnect-like."""

    name = "ankiconnect"
    supports_scheduler_introspection = False

    def __init__(self) -> None:
        self.answers: list[tuple[int, int]] = []

    def answer_card(self, *, card_id: int, ease: int) -> dict[str, Any]:
        self.answers.append((card_id, ease))
        return {"answered": True}


def _session(backend: Any, tmp_path: Path, deck: str | None = None) -> ReviewSession:
    return ReviewSession(backend, deck, undo_store=UndoStore(path=tmp_path / "undo.json"))


@pytest.mark.parametrize(
    ("text", "ease"),
    [("1", 1), ("again", 1), ("Hard", 2), (" 3 ", 3), ("GOOD", 3), ("easy", 4), ("4", 4)],
)
def test_parse_ease(text: str, ease: int) -> None:
    assert parse_ease(text) == ease


@pytest.mark.parametrize("text", ["", "0", "5", "ok", "good!"])
def test_parse_ease_rejects_anything_else(text: str) -> None:
    with pytest.raises(ValueError, match="rating must be one of"):
        parse_ease(text)


def test_deck_is_stripped_and_blank_means_all_decks(tmp_path: Path) -> None:
    assert _session(DirectBackend(), tmp_path, "  A::B ").deck == "A::B"
    assert _session(DirectBackend(), tmp_path, "   ").deck is None
    session = _session(DirectBackend(), tmp_path, "A")
    session.deck = None
    assert session.deck is None


def test_next_uses_the_schedulers_pick_on_a_direct_backend(tmp_path: Path) -> None:
    backend = DirectBackend()
    assert _session(backend, tmp_path, "Lang").next() == NextCard(7, "review_due")
    assert backend.picked_decks == ["Lang"]


def test_the_schedulers_nothing_due_is_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The query-based approximation would resurface cards the scheduler hides
    (buried, load-balanced); the inline reviewer used to fall back to it."""
    backend = DirectBackend()
    backend.next_pick = {"card_id": None, "kind": "none"}
    monkeypatch.setattr(
        session_mod,
        "pick_next_due_card_id",
        lambda *a, **k: pytest.fail("fallback must not run"),
    )
    assert _session(backend, tmp_path).next() == NextCard(None, "none")


@pytest.mark.parametrize("picked", [None, "nope", {"card_id": 0, "kind": "x"}, {"card_id": "7"}])
def test_next_tolerates_odd_scheduler_answers(tmp_path: Path, picked: Any) -> None:
    backend = DirectBackend()
    backend.next_pick = picked  # type: ignore[assignment]
    assert _session(backend, tmp_path).next().card_id is None


def test_next_falls_back_to_the_query_picker_without_introspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_pick(backend: Any, *, deck: str | None = None) -> tuple[int | None, str]:
        seen["deck"] = deck
        return 55, "review_due"

    monkeypatch.setattr(session_mod, "pick_next_due_card_id", fake_pick)
    assert _session(PlainBackend(), tmp_path, "D").next() == NextCard(55, "review_due")
    assert seen["deck"] == "D"


def test_answer_pushes_the_pre_answer_snapshot_and_revlog_id(tmp_path: Path) -> None:
    backend = DirectBackend()
    session = _session(backend, tmp_path)

    outcome = session.answer(5, 3)

    assert outcome.result == {"card_id": 5, "revlog_id": 777}
    assert outcome.undo_error is None
    assert backend.answers == [(5, 3)]
    item = session.undo_store.pop(collection="/tmp/col.anki2")
    assert item is not None and item.card_id == 5
    # The snapshot is the state *before* the answer, plus the row it wrote.
    assert item.snapshot == {"id": 5, "queue": 2, "revlog_id": 777}


def test_a_failed_answer_leaves_no_undo_entry(tmp_path: Path) -> None:
    backend = DirectBackend()
    backend.fail_answer = RuntimeError("collection is locked")
    session = _session(backend, tmp_path)

    with pytest.raises(RuntimeError, match="locked"):
        session.answer(5, 3)

    assert session.undo_store.pop(collection="/tmp/col.anki2") is None


def test_a_failed_undo_write_is_reported_not_raised(tmp_path: Path) -> None:
    """The answer is committed; raising would invite a retry that double-reviews."""

    class FailingStore:
        def push(self, item: Any, *, max_items: int = 50) -> None:
            raise OSError("no space left on device")

    session = ReviewSession(DirectBackend(), undo_store=FailingStore())  # type: ignore[arg-type]
    outcome = session.answer(5, 3)
    assert outcome.undo_error == "no space left on device"
    assert outcome.result["revlog_id"] == 777


def test_answer_without_introspection_answers_and_skips_undo(tmp_path: Path) -> None:
    backend = PlainBackend()
    session = _session(backend, tmp_path)
    outcome = session.answer(5, 2)
    assert outcome.result == {"answered": True}
    assert outcome.undo_error is None
    assert backend.answers == [(5, 2)]
    assert session.undo_store.pop(collection="") is None


def test_undo_restores_the_last_answered_card(tmp_path: Path) -> None:
    backend = DirectBackend()
    session = _session(backend, tmp_path)
    assert session.undo() is None  # nothing yet

    session.answer(5, 3)
    outcome = session.undo()

    assert outcome is not None
    assert outcome.item.card_id == 5
    assert outcome.result == {"restored": True, "card_id": 5}
    assert backend.restored == [{"id": 5, "queue": 2, "revlog_id": 777}]
    assert session.undo() is None  # consumed


def test_undo_and_preview_need_the_direct_backend(tmp_path: Path) -> None:
    session = _session(PlainBackend(), tmp_path)
    assert session.introspects is False
    with pytest.raises(IntrospectionUnavailableError):
        session.undo()
    with pytest.raises(IntrospectionUnavailableError):
        session.preview(1)


def test_preview_lists_the_four_ratings(tmp_path: Path) -> None:
    assert [i["ease"] for i in _session(DirectBackend(), tmp_path).preview(3)] == [1, 2, 3, 4]
