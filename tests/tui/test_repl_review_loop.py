"""The REPL's inline reviewer, driven without a terminal (#36)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("prompt_toolkit")
pytest.importorskip("markdownify")

import anki_cli.backends.factory as factory_mod
import anki_cli.tui.repl as repl_mod
from anki_cli.core.review_session import ReviewSession
from anki_cli.core.undo import UndoStore

pytestmark = pytest.mark.tui


class Backend:
    name = "direct"
    supports_scheduler_introspection = True
    collection_path = Path("/tmp/col.anki2")

    def __init__(self, cards: list[int]) -> None:
        self.due = list(cards)
        self.answered: list[tuple[int, int]] = []
        self.restored: list[dict[str, Any]] = []
        self.fail_answers = 0

    def get_due_counts(self, deck: str | None = None) -> dict[str, int]:
        return {"new": len(self.due), "learn": 0, "review": 0}

    def get_next_due_card(self, deck: str | None = None) -> dict[str, Any]:
        return {"card_id": self.due[0] if self.due else None, "kind": "review_due"}

    def snapshot_card_state(self, card_id: int) -> dict[str, Any]:
        return {"id": card_id}

    def answer_card(self, *, card_id: int, ease: int) -> dict[str, Any]:
        if self.fail_answers:
            self.fail_answers -= 1
            raise RuntimeError("collection is locked")
        self.answered.append((card_id, ease))
        self.due.remove(card_id)
        return {"revlog_id": card_id * 10}

    def restore_card_state(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        self.restored.append(snapshot)
        self.due.insert(0, snapshot["id"])
        return {"restored": True}


class PlainBackend(Backend):
    """AnkiConnect-like: cards come from the query-based picker."""

    supports_scheduler_introspection = False

    def find_cards(self, query: str) -> list[int]:
        return list(self.due) if "is:review" in query else []

    def get_card(self, card_id: int) -> dict[str, Any]:
        return {"cardId": card_id, "due": 1, "due_info": None}


@pytest.fixture(autouse=True)
def _render(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        repl_mod, "_render_card_inline", lambda backend, cid: (f"Q{cid}", f"A{cid}")
    )


def _run(
    backend: Backend,
    choices: list[str],
    tmp_path: Path,
    *,
    reveals: int | None = None,
) -> tuple[int, str]:
    """Run the loop with scripted ratings; returns (reviewed, console output)."""
    typed = iter(choices)
    left = {"reveals": 10_000 if reveals is None else reveals}

    def reveal() -> bool:
        left["reveals"] -= 1
        return left["reveals"] >= 0

    session = ReviewSession(backend, undo_store=UndoStore(path=tmp_path / "undo.json"))
    with repl_mod.console.capture() as cap:
        reviewed = repl_mod._review_loop(session, reveal=reveal, rate=lambda: next(typed, "q"))
    return reviewed, cap.get()


def test_rating_every_card_finishes_with_a_summary(tmp_path: Path) -> None:
    backend = Backend([1, 2])
    reviewed, out = _run(backend, ["3", "good"], tmp_path)
    assert reviewed == 2
    assert backend.answered == [(1, 3), (2, 3)]
    assert "Session complete" in out and "Reviewed 2 cards" in out
    assert "Q1" in out and "A2" in out


def test_q_stops_mid_session(tmp_path: Path) -> None:
    backend = Backend([1, 2, 3])
    reviewed, out = _run(backend, ["4", "q"], tmp_path)
    assert reviewed == 1
    assert backend.due == [2, 3]
    assert "Stopped. reviewed=1" in out
    assert "Session complete" not in out


def test_undo_takes_the_last_answer_back_and_asks_it_again(tmp_path: Path) -> None:
    backend = Backend([1, 2])
    # rate card 1, rate card 2, then (at the end of card 2's turn there is none
    # left) - so undo from inside card 2's prompt instead.
    reviewed, out = _run(backend, ["3", "u", "u", "q"], tmp_path)
    assert backend.answered == [(1, 3)]
    assert backend.restored == [{"id": 1, "revlog_id": 10}]
    assert "(undone)" in out
    assert "(nothing to undo)" in out
    assert reviewed == 0  # the one answer was undone


def test_a_bad_choice_reprompts_without_answering(tmp_path: Path) -> None:
    backend = Backend([1])
    reviewed, out = _run(backend, ["x", "9", "2"], tmp_path)
    assert out.count("(1/2/3/4/u/q)") == 2
    assert backend.answered == [(1, 2)]
    assert reviewed == 1


def test_a_failed_answer_reports_and_retries_the_same_card(tmp_path: Path) -> None:
    backend = Backend([1])
    backend.fail_answers = 1
    reviewed, out = _run(backend, ["3", "3"], tmp_path)
    assert "answer failed:" in out and "collection is locked" in out
    assert backend.answered == [(1, 3)]
    assert reviewed == 1


def test_undo_is_refused_without_the_direct_backend(tmp_path: Path) -> None:
    backend = PlainBackend([1])
    reviewed, out = _run(backend, ["u", "3"], tmp_path)
    assert "undo only available for direct backend" in out
    assert backend.answered == [(1, 3)] and reviewed == 1


def test_ctrl_d_at_the_reveal_prompt_ends_the_session(tmp_path: Path) -> None:
    backend = Backend([1, 2])
    reviewed, _ = _run(backend, ["3"], tmp_path, reveals=0)
    assert reviewed == 0
    assert backend.answered == []


def test_an_unrenderable_card_stops_instead_of_spinning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing was answered, so the scheduler would return the same card forever."""
    monkeypatch.setattr(repl_mod, "_render_card_inline", lambda backend, cid: None)
    backend = Backend([1, 2])
    reviewed, out = _run(backend, [], tmp_path)
    assert reviewed == 0
    assert "card 1 cannot be rendered" in out


def test_no_due_cards_ends_at_once(tmp_path: Path) -> None:
    reviewed, out = _run(Backend([]), [], tmp_path)
    assert reviewed == 0
    assert "Session complete" in out


def test_inline_review_closes_the_backend_session_even_when_the_loop_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    @contextmanager
    def fake_session(ctx_obj: dict[str, Any]) -> Iterator[Backend]:
        events.append("open")
        try:
            yield Backend([1])
        finally:
            events.append("close")

    def boom(session: ReviewSession, **_: Any) -> int:
        raise RuntimeError("scheduler exploded")

    monkeypatch.setattr(factory_mod, "backend_session_from_context", fake_session)
    monkeypatch.setattr(repl_mod, "_review_loop", boom)

    with repl_mod.console.capture() as cap:
        repl_mod._inline_review({}, deck=None)  # must not raise into the REPL

    assert events == ["open", "close"]
    assert "scheduler exploded" in cap.get()


def test_inline_review_reports_a_backend_that_will_not_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(ctx_obj: dict[str, Any]) -> Any:
        raise RuntimeError("Anki is not running")

    monkeypatch.setattr(factory_mod, "backend_session_from_context", refuse)
    with repl_mod.console.capture() as cap:
        repl_mod._inline_review({}, deck=None)
    assert "Anki is not running" in cap.get()


# --- dispatch: the deck context must not override an explicit deck ------------------


@pytest.mark.parametrize("spelling", [["--deck", "Mine"], ["--deck=Mine"], ["deck=Mine"]])
def test_use_context_does_not_override_an_explicit_deck(spelling: list[str]) -> None:
    parts = ["review:next", *spelling]
    assert repl_mod._prepare_line(parts, deck_context="Context") == parts


def test_use_context_still_scopes_a_line_that_names_no_deck() -> None:
    out = repl_mod._prepare_line(["review:next"], deck_context="Context")
    assert out == ["review:next", "--deck", "Context"]
