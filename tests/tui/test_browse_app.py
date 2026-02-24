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


def test_format_card_row_extracts_fields() -> None:
    card = {
        "cardId": 123,
        "deckName": "Default",
        "notetype_name": "Basic",
        "fields": ["<b>Hello</b> world", "Back side"],
        "due_info": "2024-01-01",
        "queue": 2,
        "interval": 10,
        "reps": 5,
        "lapses": 1,
    }
    row = browse_mod._format_card_row(card)
    # Row values are Rich Text objects — compare .plain for content
    assert row[0].plain == "123"
    assert row[1].plain == "Default"
    assert row[2].plain == "Basic"
    assert row[3].plain == "Hello world"
    assert row[4].plain == "2024-01-01"
    assert row[5].plain == "Review"
    assert row[6].plain == "10"
    assert row[7].plain == "5"
    assert row[8].plain == "1"


def test_format_card_row_empty_fields() -> None:
    card: dict[str, Any] = {"cardId": 1, "fields": []}
    row = browse_mod._format_card_row(card)
    assert row[3].plain == ""  # question should be empty


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


def test_format_card_row_queue_has_color_style() -> None:
    from anki_cli.tui.colors import GREEN, BLUE, RED
    card = {"cardId": 1, "queue": 2, "lapses": 0}
    row = browse_mod._format_card_row(card)
    assert row[5].plain == "Review"
    assert GREEN in str(row[5].style)

    card_new = {"cardId": 2, "queue": 0, "lapses": 0}
    row_new = browse_mod._format_card_row(card_new)
    assert row_new[5].plain == "New"
    assert BLUE in str(row_new[5].style)


def test_format_card_row_high_lapses_highlighted() -> None:
    from anki_cli.tui.colors import RED, DIM
    card_ok = {"cardId": 1, "lapses": 2}
    card_bad = {"cardId": 2, "lapses": 5}
    row_ok = browse_mod._format_card_row(card_ok)
    row_bad = browse_mod._format_card_row(card_bad)
    assert DIM in str(row_ok[8].style)
    assert RED in str(row_bad[8].style)


def test_browse_app_constructor() -> None:
    app = browse_mod.BrowseApp(backend=object(), query="deck:Test")
    assert app._query == "deck:Test"
    assert app._cards == []


def test_browse_app_constructor_default_query() -> None:
    app = browse_mod.BrowseApp(backend=object())
    assert app._query == ""
