"""#30 item 1: both backends' read APIs satisfy the same canonical models.

The direct backend is the definition; the AnkiConnect backend is normalised
to it. If either drifts, the model validation here fails.
"""

from __future__ import annotations

from typing import Any

import pytest

from anki_cli.backends.ankiconnect import AnkiConnectBackend
from anki_cli.db.anki_direct import AnkiDirectReadStore
from anki_cli.models.entities import Card, Deck, Note, Notetype
from tests.conftest import Collection

CANONICAL_CARD_KEYS = set(Card.model_fields) - {"deckId", "notetype_id", "flags"}
CANONICAL_NOTE_KEYS = {"id", "mod", "tags", "fields"}


def _direct(collection: Collection) -> AnkiDirectReadStore:
    """The seeded fixture (Default deck 1, Basic notetype 10) plus one note/card
    mirroring the AnkiConnect fake below."""
    collection.insert_note(id=100, fields=["front", "back"], tags=["t"], mod=1, usn=-1)
    collection.insert_card(
        id=200, nid=100, did=1, type=2, queue=2, due=40, ivl=16, factor=2500, reps=14,
        lapses=1, mod=1, usn=-1,
    )
    return collection.store(writable=False)


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


def test_both_backends_emit_the_canonical_card(collection, monkeypatch) -> None:
    direct = _direct(collection).get_card(200)
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


def test_both_backends_emit_the_canonical_note(collection, monkeypatch) -> None:
    direct = _direct(collection).get_note(100)
    ac = _ankiconnect(monkeypatch).get_note(100)

    for note in (direct, ac):
        Note.model_validate(note)
        assert set(note) >= CANONICAL_NOTE_KEYS
    assert direct["id"] == ac["id"] == 100
    assert direct["fields"] == ac["fields"] == ["front", "back"]
    assert direct["tags"] == ac["tags"] == ["t"]


def test_both_backends_emit_canonical_notetypes(collection, monkeypatch) -> None:
    store = _direct(collection)
    ac = _ankiconnect(monkeypatch)

    d_nt, a_nt = store.get_notetype("Basic"), ac.get_notetype("Basic")
    Notetype.model_validate(d_nt)
    Notetype.model_validate(a_nt)
    assert d_nt["id"] == a_nt["id"] == 10
    assert d_nt["kind"] == a_nt["kind"] == "normal"
    assert d_nt["fields"] == a_nt["fields"] == ["Front", "Back"]
    assert d_nt["templates"]["Card 1"]["ord"] == a_nt["templates"]["Card 1"]["ord"] == 0
    assert [n["id"] for n in store.get_notetypes()] == [n["id"] for n in ac.get_notetypes()]


def test_both_backends_emit_canonical_decks(collection, monkeypatch) -> None:
    store = _direct(collection)
    ac = _ankiconnect(monkeypatch)

    for deck in (*store.get_decks(), *ac.get_decks(), store.get_deck("Default"),
                 ac.get_deck("Default")):
        Deck.model_validate(deck)
    assert store.get_deck("Default")["kind"] == "normal"
    assert ac.get_deck("Default")["kind"] == "unknown"  # honest, not guessed
    assert store.get_deck("Default")["id"] == ac.get_deck("Default")["id"] == 1


def test_unknown_id_is_not_found_on_both_backends(collection, monkeypatch) -> None:
    """AnkiConnect returns ``{}`` for an id Anki does not know; before this fix
    the normaliser turned that into a card with ``cardId: 0`` and exit 0."""
    store = _direct(collection)
    ac = _ankiconnect(monkeypatch)
    monkeypatch.setattr(
        ac, "_invoke",
        lambda action, **p: [{}] if action in {"cardsInfo", "notesInfo"} else None,
    )

    for backend in (store, ac):
        with pytest.raises(LookupError, match="Card not found"):
            backend.get_card(424242)
        with pytest.raises(LookupError, match="Note not found"):
            backend.get_note(424242)
