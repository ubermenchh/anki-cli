from __future__ import annotations

from typing import Any

from anki_cli.cli.commands.cards import (
    _collect_card_ids,
    _extract_note_id,
    _extract_ord,
    _pick_template,
)


def test_collect_card_ids_prefers_id_over_query() -> None:
    class Backend:
        def __init__(self) -> None:
            self.called = False

        def find_cards(self, query: str) -> list[int]:
            self.called = True
            return [99]

    backend = Backend()
    ids = _collect_card_ids(backend=backend, card_id=7, query="deck:Default")

    assert ids == [7]
    assert backend.called is False


def test_collect_card_ids_uses_query_when_no_id() -> None:
    class Backend:
        def __init__(self) -> None:
            self.query: str | None = None

        def find_cards(self, query: str) -> list[int]:
            self.query = query
            return [1, 2]

    backend = Backend()
    ids = _collect_card_ids(backend=backend, card_id=None, query="tag:foo")

    assert ids == [1, 2]
    assert backend.query == "tag:foo"


def test_collect_card_ids_uses_empty_query_when_none() -> None:
    class Backend:
        def __init__(self) -> None:
            self.query: str | None = None

        def find_cards(self, query: str) -> list[int]:
            self.query = query
            return []

    backend = Backend()
    ids = _collect_card_ids(backend=backend, card_id=None, query=None)

    assert ids == []
    assert backend.query == ""


def test_extract_note_id_checks_multiple_keys_in_order() -> None:
    card = {"nid": 11, "noteId": 22, "note": 33}
    assert _extract_note_id(card) == 33  # note key is checked first


def test_extract_note_id_returns_none_when_no_int_note_id() -> None:
    card = {"note": "x", "nid": None, "noteId": 1.2, "note_id": "5"}
    assert _extract_note_id(card) is None


def test_extract_ord_returns_int_or_zero() -> None:
    assert _extract_ord({"ord": 4}) == 4
    assert _extract_ord({"ord": "4"}) == 0
    assert _extract_ord({}) == 0


def test_pick_template_prefers_explicit_ord_match() -> None:
    templates = {
        "Card A": {"ord": 2, "Front": "F2"},
        "Card B": {"ord": 0, "Front": "F0"},
    }

    picked = _pick_template(templates, ord_=0)

    assert picked is not None
    name, tmpl = picked
    assert name == "Card B"
    assert tmpl["Front"] == "F0"


def test_pick_template_falls_back_to_index_order() -> None:
    templates = {
        "Card 1": {"Front": "F1"},
        "Card 2": {"Front": "F2"},
    }

    picked = _pick_template(templates, ord_=1)

    assert picked is not None
    name, tmpl = picked
    assert name == "Card 2"
    assert tmpl["Front"] == "F2"


def test_pick_template_falls_back_to_first_item() -> None:
    templates = {
        "Card 1": {"Front": "F1"},
        "Card 2": {"Front": "F2"},
    }

    picked = _pick_template(templates, ord_=99)

    assert picked is not None
    name, tmpl = picked
    assert name == "Card 1"
    assert tmpl["Front"] == "F1"


def test_pick_template_handles_non_mapping_template_entry() -> None:
    templates: dict[str, Any] = {
        "Card 1": "not-a-mapping",
    }

    picked = _pick_template(templates, ord_=0)

    assert picked is not None
    name, tmpl = picked
    assert name == "Card 1"
    assert tmpl == {}


def test_pick_template_returns_none_for_empty_templates() -> None:
    assert _pick_template({}, ord_=0) is None