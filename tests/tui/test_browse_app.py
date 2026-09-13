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
    from anki_cli.tui.colors import BLUE, GREEN

    card = {"cardId": 1, "queue": 2, "lapses": 0}
    row = browse_mod._format_card_row(card)
    assert row[5].plain == "Review"
    assert GREEN in str(row[5].style)

    card_new = {"cardId": 2, "queue": 0, "lapses": 0}
    row_new = browse_mod._format_card_row(card_new)
    assert row_new[5].plain == "New"
    assert BLUE in str(row_new[5].style)


def test_format_card_row_high_lapses_highlighted() -> None:
    from anki_cli.tui.colors import DIM, RED

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


def test_format_due_short_handles_day_learn_like_review(monkeypatch: pytest.MonkeyPatch) -> None:
    """Day-learn cards (#19) carry a day index + epoch, not an intraday epoch."""
    now = 1_700_000_000
    monkeypatch.setattr(browse_mod.time, "time", lambda: now)

    def card(kind: str, queue: int, **due_info: int) -> dict:
        return {"queue": queue, "due_info": {"kind": kind, "day_index": 5, **due_info}}

    # Preferred: the backend's relative day count.
    assert browse_mod._format_due_short(card("learn_day_index", 3, days_from_today=1)) == "tomorrow"
    assert (
        browse_mod._format_due_short(card("review_day_index", 2, days_from_today=1)) == "tomorrow"
    )
    assert browse_mod._format_due_short(card("review_day_index", 2, days_from_today=0)) == "today"
    assert browse_mod._format_due_short(card("review_day_index", 2, days_from_today=-3)) == "today"
    assert browse_mod._format_due_short(card("review_day_index", 2, days_from_today=4)) == "4d"

    intraday = {"queue": 1, "due_info": {"kind": "learn_epoch_secs", "epoch_secs": now + 600}}
    assert browse_mod._format_due_short(intraday) == "10m"


class _CaptureStatic:
    def __init__(self, selector: str, sink: dict[str, Any]) -> None:
        self._selector = selector
        self._sink = sink

    def update(self, value: Any) -> None:
        self._sink[self._selector] = value


def _capture_updates(app: browse_mod.BrowseApp) -> dict[str, Any]:
    sink: dict[str, Any] = {}
    app.query_one = lambda sel, *a, **k: _CaptureStatic(sel, sink)  # type: ignore[method-assign]
    return sink


def test_hint_bar_lists_only_actual_bindings() -> None:
    app = browse_mod.BrowseApp(backend=object())
    captured = _capture_updates(app)

    app._render_hint_bar()
    text = captured["#hintbar"].plain

    # Exact text so key/label swaps, dropped separators and hidden bindings
    # leaking in all fail.
    assert text == (
        " /  Search    Tab  Cycle filter    Enter  Detail    d  Delete"
        "    s  Suspend    r  Refresh    q  Quit"
    )

    # Phantom keys previously advertised must be gone.
    for phantom in ("add", "edit"):
        assert phantom not in text


def test_browse_has_no_edit_binding_or_stub_action() -> None:
    actions = {b.action for b in browse_mod.BrowseApp.BINDINGS}
    assert "edit_selected" not in actions
    assert not hasattr(browse_mod.BrowseApp, "action_edit_selected")


def test_preview_actions_label_enter_as_detail() -> None:
    app = browse_mod.BrowseApp(backend=object())
    captured = _capture_updates(app)

    app._render_preview_empty()
    actions = captured["#preview-actions"].plain

    assert "Detail" in actions
    assert "Study" not in actions
    assert "Edit" not in actions


def test_preview_actions_for_card_show_detail_and_unsuspend() -> None:
    app = browse_mod.BrowseApp(backend=object())
    app._visible_cards = [
        {"cardId": 1, "queue": -1, "fields": ["Q", "A"], "tags": []},
    ]
    captured = _capture_updates(app)

    app._update_preview_for_row(0)
    actions = captured["#preview-actions"].plain

    assert "Unsuspend" in actions
    assert "Detail" in actions
    assert "Study" not in actions
    assert "Edit" not in actions


def test_format_due_short_epoch_fallback_rounds_up_to_the_due_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#21: epoch_secs is the *start* of the due scheduling day. A rollover 21 h
    away is tomorrow, and flooring (epoch - now) // 86400 wrongly said today."""
    now = 1_700_000_000
    monkeypatch.setattr(browse_mod.time, "time", lambda: now)

    def review(epoch: int) -> dict:
        return {
            "queue": 2,
            "due_info": {"kind": "review_day_index", "day_index": 5, "epoch_secs": epoch},
        }

    assert browse_mod._format_due_short(review(now + 21 * 3600)) == "tomorrow"
    assert browse_mod._format_due_short(review(now + 86_400)) == "tomorrow"
    assert browse_mod._format_due_short(review(now + 86_400 + 60)) == "2d"
    assert browse_mod._format_due_short(review(now - 3600)) == "today"  # started already
    assert browse_mod._format_due_short(review(now - 5 * 86_400)) == "today"  # overdue
