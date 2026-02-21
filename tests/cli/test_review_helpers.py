from __future__ import annotations

from typing import Any

import pytest

from anki_cli.cli.commands.review import (
    _extract_note_id,
    _extract_ord,
    _parse_ease,
    _pick_template,
)


@pytest.mark.parametrize(
    ("rating", "expected"),
    [
        ("1", 1),
        ("2", 2),
        ("3", 3),
        ("4", 4),
        ("again", 1),
        ("hard", 2),
        ("good", 3),
        ("easy", 4),
        ("  GOOD  ", 3),
    ],
)
def test_parse_ease_valid(rating: str, expected: int) -> None:
    assert _parse_ease(rating) == expected


@pytest.mark.parametrize("rating", ["0", "5", "bad", "", "  "])
def test_parse_ease_invalid(rating: str) -> None:
    with pytest.raises(ValueError, match="rating must be one of"):
        _parse_ease(rating)


def test_extract_note_id_prefers_first_known_key_order() -> None:
    card = {"nid": 22, "note": 11, "noteId": 33, "note_id": 44}
    assert _extract_note_id(card) == 11


def test_extract_note_id_returns_none_when_no_int() -> None:
    card = {"note": "x", "nid": None, "noteId": 1.2}
    assert _extract_note_id(card) is None


def test_extract_ord_returns_int_or_zero() -> None:
    assert _extract_ord({"ord": 3}) == 3
    assert _extract_ord({"ord": "3"}) == 0
    assert _extract_ord({}) == 0


def test_pick_template_prefers_explicit_ord_field() -> None:
    templates = {
        "Card A": {"ord": 2, "Front": "F2"},
        "Card B": {"ord": 1, "Front": "F1"},
    }

    picked = _pick_template(templates, ord_=1)

    assert picked is not None
    assert picked["Front"] == "F1"


def test_pick_template_falls_back_to_index() -> None:
    templates = {
        "Card 1": {"Front": "F1"},
        "Card 2": {"Front": "F2"},
    }

    picked = _pick_template(templates, ord_=1)

    assert picked is not None
    assert picked["Front"] == "F2"


def test_pick_template_falls_back_to_first() -> None:
    templates = {
        "Card 1": {"Front": "F1"},
        "Card 2": {"Front": "F2"},
    }

    picked = _pick_template(templates, ord_=99)

    assert picked is not None
    assert picked["Front"] == "F1"


def test_pick_template_handles_non_mapping_entry() -> None:
    templates: dict[str, Any] = {"Card 1": "not-a-mapping"}

    picked = _pick_template(templates, ord_=0)

    assert picked == {}


def test_pick_template_empty_returns_none() -> None:
    assert _pick_template({}, ord_=0) is None