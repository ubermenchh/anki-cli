"""``backends/normalize.py`` in isolation: AnkiConnect raw rows -> canonical
keys, additive (AnkiConnect's own keys survive), validated by the models."""

from __future__ import annotations

from typing import Any

import pytest

from anki_cli.backends.normalize import (
    normalize_card,
    normalize_deck,
    normalize_note,
    normalize_notetype,
    ordered_fields,
)
from anki_cli.models.entities import Card, Deck, Note, Notetype

RAW_CARD: dict[str, Any] = {
    "cardId": 1498938915662, "note": 1502298033753, "deckName": "Default",
    "modelName": "Basic", "fieldOrder": 0, "ord": 0,
    "fields": {"Back": {"value": "back", "order": 1}, "Front": {"value": "front", "order": 0}},
    "question": "front", "answer": "front<hr id=answer>back", "css": ".card {}",
    "type": 2, "queue": 2, "due": 1234, "interval": 16, "factor": 2500,
    "reps": 14, "lapses": 1, "left": 0, "mod": 1629454092,
    "nextReviews": ["<1m", "<10m", "4d", "9.3mo"],
}
RAW_NOTE: dict[str, Any] = {
    "noteId": 1502298033753, "modelName": "Basic", "tags": ["tag", "another_tag"],
    "fields": {"Back": {"value": "back", "order": 1}, "Front": {"value": "front", "order": 0}},
    "cards": [1498938915662], "mod": 1718377864, "profile": "User 1",
}


def test_ordered_fields_sorts_by_order_and_unwraps_value() -> None:
    assert ordered_fields(RAW_CARD["fields"]) == (["Front", "Back"], ["front", "back"])
    assert ordered_fields({"A": "plain", "B": {"value": "v"}}) == (["A", "B"], ["plain", "v"])
    assert ordered_fields("not a mapping") == ([], [])


def test_normalize_card_adds_canonical_keys_and_keeps_ankiconnect_ones() -> None:
    card = normalize_card(RAW_CARD)

    assert card["notetype_name"] == "Basic" and card["modelName"] == "Basic"
    assert card["fields"] == ["front", "back"]
    assert card["field_names"] == ["Front", "Back"]
    assert card["tags"] == []
    assert card["due_info"] == {"kind": "review_day_index", "raw": 1234, "day_index": 1234}
    assert card["left_info"] == {"raw": 0, "today_remaining": 0, "until_graduation": 0}
    for keep in ("question", "answer", "css", "nextReviews", "mod", "fieldOrder"):
        assert card[keep] == RAW_CARD[keep]
    Card.model_validate(card)  # canonical shape


@pytest.mark.parametrize(
    ("type_", "queue", "due", "expected"),
    [
        (0, 0, 7, {"kind": "new_position", "raw": 7, "position": 7}),
        (1, 1, 1_700_000_600, {"kind": "learn_epoch_secs", "raw": 1_700_000_600,
                                "epoch_secs": 1_700_000_600}),
        (1, 3, 130, {"kind": "learn_day_index", "raw": 130, "day_index": 130}),
        (3, 1, 1_700_000_600, {"kind": "learn_epoch_secs", "raw": 1_700_000_600,
                                "epoch_secs": 1_700_000_600}),
        (2, 2, 40, {"kind": "review_day_index", "raw": 40, "day_index": 40}),
        (9, 4, 5, {"kind": "raw", "raw": 5, "queue": 4, "type": 9}),
    ],
)
def test_normalize_card_due_info_matches_direct_decoding_without_timing(
    type_, queue, due, expected
) -> None:
    """Same rules as the direct backend's ``due_info`` (``core.due.decode_due``);
    no ``epoch_secs``/``days_from_today`` for day-index kinds because AnkiConnect
    does not expose the collection's rollover timing."""
    card = normalize_card({**RAW_CARD, "type": type_, "queue": queue, "due": due})
    assert card["due_info"] == expected


def test_normalize_card_defaults_optional_keys_but_the_backend_owns_not_found() -> None:
    """The normaliser fills gaps so a partial row still validates; deciding that
    a row means "no such card" (AnkiConnect returns ``{}``) is the backend's job
    — see test_ankiconnect_deck_notetype_paths for that."""
    card = normalize_card({"cardId": 7})
    assert card["note"] == 0 and card["fields"] == [] and card["notetype_name"] == ""
    Card.model_validate(card)


def test_normalize_note() -> None:
    note = normalize_note(RAW_NOTE)
    assert note["id"] == RAW_NOTE["noteId"] and note["noteId"] == RAW_NOTE["noteId"]
    assert note["notetype_name"] == "Basic"
    assert note["fields"] == ["front", "back"] and note["field_names"] == ["Front", "Back"]
    assert note["tags"] == ["tag", "another_tag"] and note["mod"] == RAW_NOTE["mod"]
    assert note["cards"] == [1498938915662]  # kept
    Note.model_validate(note)


def test_normalize_deck_and_notetype() -> None:
    deck = normalize_deck({"id": 1, "name": "D"})
    assert deck == {"id": 1, "name": "D", "kind": "unknown"}
    Deck.model_validate(deck)
    Deck.model_validate(normalize_deck({"id": 1, "name": "D", "due_counts": {"new": 1}}))

    nt = normalize_notetype({
        "id": 5, "name": "Basic", "kind": "normal", "fields": ["Front", "Back"],
        "templates": {"Card 1": {"Front": "{{Front}}", "Back": "{{Back}}"},
                      "Card 2": {"Front": "{{Back}}", "Back": "{{Front}}", "ord": 7}},
        "styling": {"css": ".card {}"},
    })
    # ord added in insertion order; an explicit ord is respected.
    assert nt["templates"]["Card 1"]["ord"] == 0
    assert nt["templates"]["Card 2"]["ord"] == 7
    Notetype.model_validate(nt)

    bare = normalize_notetype({"name": "X", "fields": []})
    assert bare["templates"] == {} and bare["styling"] == {} and bare["kind"] == "normal"
