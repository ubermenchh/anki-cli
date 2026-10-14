"""Shared fake backend for the Pilot-driven TUI tests (``tests.tui.fake_backend``).

``FakeBackend`` speaks the canonical card/notetype shape both real backends
emit (``backends/normalize.py``) so ``render_card`` and the table formatters run
the same code paths they do in production. Pure-function tests in this
directory do not need it; the ``test_pilot_*`` files mount the apps for real
via ``App.run_test()`` and drive them with key presses.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("textual")


def make_card(i: int, *, front: str = "The capital of France") -> dict[str, Any]:
    queue = (2, 0, 1, -1)[i % 4]
    return {
        "cardId": 1000 + i,
        "note": 5000 + i,
        "deckName": "Languages::Japanese::Core 2k",
        "notetype_name": "Basic",
        "ord": 0,
        "type": 2 if queue == 2 else 0,
        "queue": queue,
        "due": 19000 + i,
        "interval": 12 if queue == 2 else 0,
        "factor": 2500,
        "reps": 5,
        "lapses": 0,
        "left": 0,
        "flags": 0,
        "fields": [front, "Paris"],
        "field_names": ["Front", "Back"],
        "tags": ["geo"],
        "due_info": {"kind": "review_day_index", "days": 0}
        if queue == 2
        else {"kind": "new_position"},
    }


class FakeBackend:
    """Direct-backend lookalike: scheduler introspection on, every call recorded."""

    supports_scheduler_introspection = True
    collection_path = "/tmp/fake.anki2"

    def __init__(self, cards: list[dict[str, Any]] | None = None) -> None:
        self.cards = cards if cards is not None else [make_card(i) for i in range(6)]
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.snapshot_error: Exception | None = None

    def _rec(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))

    def get_due_counts(self, deck: str | None = None) -> dict[str, int]:
        return {"new": 12, "learning": 3, "review": 47}

    def get_next_due_card(self, deck: str | None = None) -> dict[str, Any]:
        return {"card_id": self.cards[0]["cardId"], "kind": "review_due"}

    def find_cards(self, query: str) -> list[int]:
        self._rec("find_cards", query)
        return [c["cardId"] for c in self.cards]

    def get_card(self, card_id: int) -> dict[str, Any]:
        return next(c for c in self.cards if c["cardId"] == card_id)

    def get_cards(self, card_ids: list[int]) -> list[dict[str, Any]]:
        self._rec("get_cards", tuple(card_ids))
        by_id = {c["cardId"]: c for c in self.cards}
        return [by_id[i] for i in card_ids if i in by_id]

    def get_note_fields(self, note_id: int, fields: list[str] | None = None) -> dict[str, str]:
        card = next(c for c in self.cards if c["note"] == note_id)
        return dict(zip(card["field_names"], card["fields"], strict=True))

    def get_notetype(self, name: str) -> dict[str, Any]:
        return {
            "name": "Basic",
            "kind": "normal",
            "templates": {
                "Card 1": {
                    "ord": 0,
                    "Front": "{{Front}}",
                    "Back": "{{FrontSide}}<hr id=answer>{{Back}}",
                }
            },
        }

    def snapshot_card_state(self, card_id: int) -> dict[str, Any]:
        if self.snapshot_error is not None:
            raise self.snapshot_error
        return {"card_id": card_id}

    def answer_card(self, card_id: int, ease: int) -> dict[str, Any]:
        self._rec("answer_card", card_id, ease)
        return {"revlog_id": 1}

    def preview_ratings(self, card_id: int) -> list[dict[str, Any]]:
        return [
            {"ease": 1, "due_info": {"kind": "learn_epoch_secs"}},
            {"ease": 2, "interval": 36},
            {"ease": 3, "due_info": {"kind": "review_day_index", "days": 72}},
            {"ease": 4, "due_info": {"kind": "review_day_index", "days": 153}},
        ]

    def suspend_cards(self, card_ids: list[int]) -> dict[str, Any]:
        self._rec("suspend_cards", tuple(card_ids))
        return {"suspended": len(card_ids)}

    def unsuspend_cards(self, card_ids: list[int]) -> dict[str, Any]:
        self._rec("unsuspend_cards", tuple(card_ids))
        return {"unsuspended": len(card_ids)}

    def delete_notes(self, note_ids: list[int]) -> dict[str, Any]:
        self._rec("delete_notes", tuple(note_ids))
        self.cards = [c for c in self.cards if c["note"] not in note_ids]
        return {"deleted": len(note_ids)}
