from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("textual")

import anki_cli.tui.browse_app as browse_mod

pytestmark = pytest.mark.tui


def test_strip_html_basic_br_becomes_space() -> None:
    out = browse_mod._strip_html_basic("hello<br>world<br/>end")
    assert out == "hello world end"


def test_strip_html_basic_removes_tags_and_unescapes() -> None:
    out = browse_mod._strip_html_basic("<p>A &amp; B</p>")
    assert out == "A & B"


def test_strip_html_basic_empty_string() -> None:
    assert browse_mod._strip_html_basic("") == ""


def test_truncate_short_string_unchanged() -> None:
    assert browse_mod._truncate("short", 80) == "short"


def test_truncate_long_string_adds_ellipsis() -> None:
    long = "x" * 100
    result = browse_mod._truncate(long, 80)
    assert len(result) == 80
    assert result.endswith("\u2026")


def test_queue_labels_mapping() -> None:
    assert browse_mod.QUEUE_LABELS[0] == "New"
    assert browse_mod.QUEUE_LABELS[1] == "Learn"
    assert browse_mod.QUEUE_LABELS[2] == "Review"
    assert browse_mod.QUEUE_LABELS[-1] == "Suspended"
    assert browse_mod.QUEUE_LABELS[-2] == "Buried"


def test_format_card_detail_includes_all_info() -> None:
    card = {
        "cardId": 42,
        "note": 100,
        "deckName": "Test",
        "notetype_name": "Basic",
        "ord": 0,
        "type": 1,
        "queue": 0,
        "due_info": "new",
        "interval": 0,
        "factor": 2500,
        "reps": 0,
        "lapses": 0,
        "flags": 0,
        "fields": ["<b>Q</b>", "A"],
        "tags": ["tag1", "tag2"],
    }
    detail = browse_mod._format_card_detail(card)
    assert "Card ID:    42" in detail
    assert "Note ID:    100" in detail
    assert "Deck:       Test" in detail
    assert "Queue:      New (0)" in detail
    assert "[0] Q" in detail
    assert "[1] A" in detail
    assert "Tags: tag1, tag2" in detail


def test_format_card_detail_no_tags_or_fields() -> None:
    card: dict[str, Any] = {"cardId": 1, "queue": 2}
    detail = browse_mod._format_card_detail(card)
    assert "Card ID:    1" in detail
    assert "Tags:" not in detail


def test_browse_app_constructor() -> None:
    app = browse_mod.BrowseApp(backend=object(), query="deck:Test")
    assert app._query == "deck:Test"
    assert app._cards == []


def test_browse_app_constructor_default_query() -> None:
    app = browse_mod.BrowseApp(backend=object())
    assert app._query == ""

def test_extract_field_values_ankiconnect_mapping_by_order() -> None:
    card: dict[str, Any] = {
        "fields": {
            "Back": {"value": "A", "order": 1},
            "Front": {"value": "<b>Q</b>", "order": 0},
        }
    }

    assert browse_mod._extract_field_values(card) == ["<b>Q</b>", "A"]
    assert browse_mod._extract_front_back(card) == ("Q", "A")


def test_format_browser_row_uses_model_name_and_mapping_fields() -> None:
    card: dict[str, Any] = {
        "deckName": "Default",
        "modelName": "Basic",
        "fields": {
            "Front": {"value": "<b>Hello</b> world", "order": 0},
            "Back": {"value": "Back side", "order": 1},
        },
        "queue": 2,
        "interval": 8,
        "due_info": "",
    }

    row = browse_mod._format_browser_row(card)
    assert row[0].plain == "Default"
    assert row[1].plain == "Basic"
    assert row[2].plain == "Hello world"


def test_extract_field_values_mapping_without_order_uses_values() -> None:
    card: dict[str, Any] = {
        "fields": {
            "Front": {"value": "Q"},
            "Back": {"value": "A"},
        }
    }

    assert browse_mod._extract_field_values(card) == ["Q", "A"]


def test_extract_field_values_mapping_plain_values() -> None:
    card: dict[str, Any] = {"fields": {"Front": "Q", "Back": "A"}}
    assert browse_mod._extract_field_values(card) == ["Q", "A"]


def test_format_card_detail_renders_mapping_fields() -> None:
    card: dict[str, Any] = {
        "cardId": 42,
        "queue": 2,
        "fields": {
            "Front": {"value": "<b>Q</b>", "order": 0},
            "Back": {"value": "A", "order": 1},
        },
    }

    detail = browse_mod._format_card_detail(card)
    assert "[0] Q" in detail
    assert "[1] A" in detail
