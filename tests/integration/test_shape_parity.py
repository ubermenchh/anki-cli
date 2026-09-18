"""#30 item 1: both backends' read APIs satisfy the same canonical models.

The direct backend is the definition; the AnkiConnect backend is normalised
to it. If either drifts, the model validation here fails.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from anki_cli.backends.ankiconnect import AnkiConnectBackend
from anki_cli.db.anki_direct import AnkiDirectReadStore
from anki_cli.models.entities import Card, Deck, Note, Notetype
from tests.integration.test_anki_direct_card_note_revlog_reads import (
    _insert_card,
    _insert_deck,
    _insert_note,
    _insert_notetype,
    _make_store,
)

CANONICAL_CARD_KEYS = set(Card.model_fields) - {"deckId", "notetype_id", "flags"}
CANONICAL_NOTE_KEYS = {"id", "mod", "tags", "fields"}


def _direct(tmp_path: Path) -> AnkiDirectReadStore:
    store, db_path = _make_store(tmp_path)
    _insert_deck(db_path, did=1, name="Default")
    _insert_notetype(db_path, ntid=10, name="Basic")
    _insert_note(
        db_path, note_id=100, guid="g", mid=10, mod=1, usn=-1, tags=" t ",
        flds="front\x1fback", sfld="front", csum=0, flags=0, data="",
    )
    _insert_card(
        db_path, card_id=200, nid=100, did=1, ord_=0, mod=1, usn=-1, card_type=2, queue=2,
        due=40, ivl=16, factor=2500, reps=14, lapses=1, left=0, odue=0, odid=0, flags=0,
        data="{}",
    )
    return store


def _ankiconnect(monkeypatch: pytest.MonkeyPatch) -> AnkiConnectBackend:
    backend = AnkiConnectBackend.__new__(AnkiConnectBackend)

    def fake_invoke(action: str, **params: Any) -> Any:
        return {
            "cardsInfo": [{"cardId": 200, "note": 100, "deckName": "Default",
                           "modelName": "Basic", "ord": 0, "type": 2, "queue": 2, "due": 40,
                           "interval": 16, "factor": 2500, "reps": 14, "lapses": 1, "left": 0,
                           "fields": {"Front": {"value": "front", "order": 0},
                                      "Back": {"value": "back", "order": 1}},
                           "question": "front", "answer": "back", "css": "", "mod": 1}],
            "notesInfo": [{"noteId": 100, "modelName": "Basic", "tags": ["t"], "mod": 1,
                           "fields": {"Front": {"value": "front", "order": 0},
                                      "Back": {"value": "back", "order": 1}},
                           "cards": [200]}],
            "deckNamesAndIds": {"Default": 1},
            "modelNamesAndIds": {"Basic": 10},
            "modelFieldNames": ["Front", "Back"],
            "modelTemplates": {"Card 1": {"Front": "{{Front}}", "Back": "{{Back}}"}},
            "modelStyling": {"css": ".card {}"},
            "findCards": [],
        }[action]

    monkeypatch.setattr(backend, "_invoke", fake_invoke)
    monkeypatch.setattr(backend, "get_due_counts", lambda deck=None: {"new": 0, "learn": 0,
                                                                        "review": 0, "total": 0})
    return backend


def test_both_backends_emit_the_canonical_card(tmp_path, monkeypatch) -> None:
    direct = _direct(tmp_path).get_card(200)
    ac = _ankiconnect(monkeypatch).get_card(200)

    for card in (direct, ac):
        Card.model_validate(card)
        assert set(card) >= CANONICAL_CARD_KEYS, CANONICAL_CARD_KEYS - set(card)

    # The values agree where AnkiConnect has the data.
    for key in ("cardId", "note", "deckName", "notetype_name", "ord", "type", "queue", "due",
                "interval", "factor", "reps", "lapses", "left", "fields"):
        assert direct[key] == ac[key], key
    assert direct["due_info"]["kind"] == ac["due_info"]["kind"] == "review_day_index"
    assert direct["due_info"]["day_index"] == ac["due_info"]["day_index"] == 40
    # Direct knows the rollover timing; AnkiConnect cannot.
    assert "epoch_secs" in direct["due_info"]
    assert "epoch_secs" not in ac["due_info"]


def test_both_backends_emit_the_canonical_note(tmp_path, monkeypatch) -> None:
    direct = _direct(tmp_path).get_note(100)
    ac = _ankiconnect(monkeypatch).get_note(100)

    for note in (direct, ac):
        Note.model_validate(note)
        assert set(note) >= CANONICAL_NOTE_KEYS
    assert direct["id"] == ac["id"] == 100
    assert direct["fields"] == ac["fields"] == ["front", "back"]
    assert direct["tags"] == ac["tags"] == ["t"]


def test_ankiconnect_notetypes_are_canonical(monkeypatch) -> None:
    # The direct side is validated in test_anki_direct_read_apis (full DDL);
    # this fixture's minimal ``notetypes`` table cannot serve get_notetype (#37).
    ac = _ankiconnect(monkeypatch)
    nt = ac.get_notetype("Basic")
    Notetype.model_validate(nt)
    assert nt["id"] == 10 and nt["kind"] == "normal"
    assert nt["templates"]["Card 1"]["ord"] == 0
    assert [n["id"] for n in ac.get_notetypes()] == [10]


def test_ankiconnect_decks_are_canonical(monkeypatch) -> None:
    # The direct side is pinned by test_anki_direct_read_apis (full deck DDL);
    # this fixture's minimal ``decks`` table cannot serve get_decks (#37).
    ac = _ankiconnect(monkeypatch)
    for deck in (*ac.get_decks(), ac.get_deck("Default")):
        Deck.model_validate(deck)
    assert ac.get_deck("Default")["kind"] == "unknown"  # honest, not guessed
