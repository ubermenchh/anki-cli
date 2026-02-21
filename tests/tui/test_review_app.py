from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

pytest.importorskip("textual")

import anki_cli.tui.review_app as review_mod

pytestmark = pytest.mark.tui


def test_strip_html_basic_converts_tags_breaks_and_entities() -> None:
    out = review_mod._strip_html_basic("<p>A<br>B<hr/>C &amp; D</p>")
    assert out == "A\nB\n----------------------------------------\nC & D"


def test_extract_note_id_prefers_primary_key_and_requires_int() -> None:
    assert review_mod._extract_note_id({"note": 1, "nid": 2}) == 1
    assert review_mod._extract_note_id({"nid": 2}) == 2
    assert review_mod._extract_note_id({"noteId": 3}) == 3
    assert review_mod._extract_note_id({"note_id": 4}) == 4
    assert review_mod._extract_note_id({"note": "1"}) is None
    assert review_mod._extract_note_id({}) is None


def test_extract_ord_defaults_to_zero_when_missing_or_non_int() -> None:
    assert review_mod._extract_ord({"ord": 2}) == 2
    assert review_mod._extract_ord({"ord": "2"}) == 0
    assert review_mod._extract_ord({}) == 0


def test_pick_template_prefers_explicit_ord_then_index_then_first() -> None:
    templates: Mapping[str, Any] = {
        "A": {"ord": 0, "Front": "F0"},
        "B": {"ord": 1, "Front": "F1"},
    }
    picked = review_mod._pick_template(templates, 1)
    assert picked is not None
    assert picked["Front"] == "F1"

    # No explicit ord fields -> fallback to insertion-order index
    no_ord: Mapping[str, Any] = {
        "First": {"Front": "T1"},
        "Second": {"Front": "T2"},
    }
    picked2 = review_mod._pick_template(no_ord, 1)
    assert picked2 is not None
    assert picked2["Front"] == "T2"

    # Out of range -> first template
    picked3 = review_mod._pick_template(no_ord, 99)
    assert picked3 is not None
    assert picked3["Front"] == "T1"

    assert review_mod._pick_template({}, 0) is None


def test_render_card_normal_hidden_and_revealed_answer() -> None:
    class Backend:
        def get_card(self, card_id: int) -> dict[str, Any]:
            return {"id": card_id, "note": 10, "ord": 0, "notetype_name": "Basic"}

        def get_note_fields(self, note_id: int, fields: list[str] | None = None) -> dict[str, str]:
            assert note_id == 10
            assert fields is None
            return {"Front": "Q", "Back": "A"}

        def get_note(self, note_id: int) -> dict[str, Any]:
            raise AssertionError("get_note should not be needed when notetype_name exists")

        def get_notetype(self, name: str) -> dict[str, Any]:
            assert name == "Basic"
            return {
                "kind": "normal",
                "templates": {"Card 1": {"Front": "{{Front}}", "Back": "{{Back}}"}},
            }

    app = review_mod.ReviewApp(backend=Backend(), deck=None)

    hidden = app._render_card(1, reveal_answer=False)
    shown = app._render_card(1, reveal_answer=True)

    assert hidden == {"question": "Q", "answer": ""}
    assert shown == {"question": "Q", "answer": "A"}


def test_render_card_falls_back_to_note_model_name() -> None:
    class Backend:
        def get_card(self, card_id: int) -> dict[str, Any]:
            return {"id": card_id, "nid": 20, "ord": 0}

        def get_note_fields(self, note_id: int, fields: list[str] | None = None) -> dict[str, str]:
            assert note_id == 20
            return {"Front": "Hello", "Back": "World"}

        def get_note(self, note_id: int) -> dict[str, Any]:
            assert note_id == 20
            return {"modelName": "Basic"}

        def get_notetype(self, name: str) -> dict[str, Any]:
            assert name == "Basic"
            return {
                "kind": "normal",
                "templates": {"Card 1": {"Front": "{{Front}}", "Back": "{{Back}}"}},
            }

    app = review_mod.ReviewApp(backend=Backend(), deck=None)
    rendered = app._render_card(2, reveal_answer=True)
    assert rendered == {"question": "Hello", "answer": "World"}


def test_render_card_cloze_uses_ord_plus_one_index() -> None:
    class Backend:
        def get_card(self, card_id: int) -> dict[str, Any]:
            return {"id": card_id, "note": 30, "ord": 0, "notetype_name": "Cloze"}

        def get_note_fields(self, note_id: int, fields: list[str] | None = None) -> dict[str, str]:
            return {"Text": "{{c1::Paris}} is in {{c2::France}}"}

        def get_note(self, note_id: int) -> dict[str, Any]:
            return {"modelName": "Cloze"}

        def get_notetype(self, name: str) -> dict[str, Any]:
            return {
                "kind": "cloze",
                "templates": {"Cloze": {"Front": "{{cloze:Text}}", "Back": "{{cloze:Text}}"}},
            }

    app = review_mod.ReviewApp(backend=Backend(), deck=None)
    rendered = app._render_card(3, reveal_answer=True)

    assert rendered["question"] == "[...] is in France"
    assert rendered["answer"] == "Paris is in France"


def test_render_card_missing_note_id_raises() -> None:
    class Backend:
        def get_card(self, card_id: int) -> dict[str, Any]:
            return {"id": card_id, "ord": 0}

    app = review_mod.ReviewApp(backend=Backend(), deck=None)

    with pytest.raises(RuntimeError, match="card has no note id"):
        app._render_card(4, reveal_answer=True)


def test_render_card_no_templates_raises() -> None:
    class Backend:
        def get_card(self, card_id: int) -> dict[str, Any]:
            return {"id": card_id, "note": 40, "ord": 0, "notetype_name": "Basic"}

        def get_note_fields(self, note_id: int, fields: list[str] | None = None) -> dict[str, str]:
            return {"Front": "Q", "Back": "A"}

        def get_note(self, note_id: int) -> dict[str, Any]:
            return {"modelName": "Basic"}

        def get_notetype(self, name: str) -> dict[str, Any]:
            return {"kind": "normal", "templates": {}}

    app = review_mod.ReviewApp(backend=Backend(), deck=None)

    with pytest.raises(RuntimeError, match="no templates found"):
        app._render_card(5, reveal_answer=True)


def test_run_command_parse_error_sets_status() -> None:
    app = review_mod.ReviewApp(backend=object(), deck=None)
    statuses: list[str] = []

    def capture(msg: str) -> None:
        statuses.append(msg)

    app._set_status = capture  # type: ignore[method-assign]
    app._run_command('rate "unterminated')

    assert statuses
    assert statuses[-1].startswith("parse error:")


def test_run_command_help_pushes_preview_screen() -> None:
    app = review_mod.ReviewApp(backend=object(), deck=None)
    pushed: list[review_mod.PreviewScreen] = []

    def capture(screen: review_mod.PreviewScreen) -> None:
        pushed.append(screen)

    app.push_screen = capture  # type: ignore[method-assign]
    app._run_command("help")

    assert len(pushed) == 1
    assert isinstance(pushed[0], review_mod.PreviewScreen)


def test_run_command_deck_sets_filter_and_loads_next() -> None:
    app = review_mod.ReviewApp(backend=object(), deck=None)
    calls = {"load_next": 0}

    def load_next() -> None:
        calls["load_next"] += 1

    app._load_next = load_next  # type: ignore[method-assign]
    app._run_command('deck "  A::B  "')
    assert app._deck == "A::B"
    assert calls["load_next"] == 1

    app._run_command("deck")
    assert app._deck is None
    assert calls["load_next"] == 2


def test_run_command_show_and_hide_toggle_answer_when_card_loaded() -> None:
    app = review_mod.ReviewApp(backend=object(), deck=None)
    app._card_id = 123
    app._show_answer = False
    calls = {"render": 0}

    def render() -> None:
        calls["render"] += 1

    app._render_current = render  # type: ignore[method-assign]

    app._run_command("show")
    assert app._show_answer is True
    app._run_command("hide")
    assert app._show_answer is False
    assert calls["render"] == 2


def test_run_command_rate_aliases_map_to_expected_ease() -> None:
    app = review_mod.ReviewApp(backend=object(), deck=None)
    called: list[int] = []

    def rate(ease: int) -> None:
        called.append(ease)

    app.action_rate = rate  # type: ignore[method-assign]

    for cmd in ("again", "hard", "good", "easy"):
        app._run_command(cmd)

    assert called == [1, 2, 3, 4]


def test_run_command_rate_usage_validation() -> None:
    app = review_mod.ReviewApp(backend=object(), deck=None)
    statuses: list[str] = []

    def capture(msg: str) -> None:
        statuses.append(msg)

    app._set_status = capture  # type: ignore[method-assign]

    app._run_command("rate")
    app._run_command("rate nope")
    app._run_command("rate 9")

    assert statuses == [
        "usage: :rate 1|2|3|4",
        "usage: :rate 1|2|3|4",
        "usage: :rate 1|2|3|4",
    ]


def test_run_command_dispatches_next_undo_preview_and_unknown() -> None:
    app = review_mod.ReviewApp(backend=object(), deck=None)
    calls = {"next": 0, "undo": 0, "preview": 0}
    statuses: list[str] = []

    def load_next() -> None:
        calls["next"] += 1

    def undo() -> None:
        calls["undo"] += 1

    def preview() -> None:
        calls["preview"] += 1

    def capture(msg: str) -> None:
        statuses.append(msg)

    app._load_next = load_next  # type: ignore[method-assign]
    app.action_undo = undo  # type: ignore[method-assign]
    app.action_preview = preview  # type: ignore[method-assign]
    app._set_status = capture  # type: ignore[method-assign]

    app._run_command("next")
    app._run_command("undo")
    app._run_command("preview")
    app._run_command("unknown-cmd")

    assert calls == {"next": 1, "undo": 1, "preview": 1}
    assert statuses[-1] == "unknown command: unknown-cmd (try :help)"


def test_action_rate_requires_answer_first_then_answers() -> None:
    class Backend:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []

        def answer_card(self, *, card_id: int, ease: int) -> None:
            self.calls.append((card_id, ease))

    backend = Backend()
    app = review_mod.ReviewApp(backend=backend, deck=None)
    app._card_id = 77
    app._show_answer = False

    statuses: list[str] = []
    calls = {"render": 0, "load_next": 0}

    def capture(msg: str) -> None:
        statuses.append(msg)

    def render() -> None:
        calls["render"] += 1

    def load_next() -> None:
        calls["load_next"] += 1

    app._set_status = capture  # type: ignore[method-assign]
    app._render_current = render  # type: ignore[method-assign]
    app._load_next = load_next  # type: ignore[method-assign]

    # First press only reveals answer.
    app.action_rate(3)
    assert app._show_answer is True
    assert backend.calls == []
    assert calls["render"] == 1
    assert statuses[-1] == "Answer shown. Press 1-4 to rate."

    # Second press submits answer.
    app.action_rate(3)
    assert backend.calls == [(77, 3)]
    assert calls["load_next"] == 1


def test_action_rate_reports_backend_failure() -> None:
    class Backend:
        def answer_card(self, *, card_id: int, ease: int) -> None:
            raise RuntimeError("boom")

    app = review_mod.ReviewApp(backend=Backend(), deck=None)
    app._card_id = 88
    app._show_answer = True

    statuses: list[str] = []
    calls = {"load_next": 0}

    def capture(msg: str) -> None:
        statuses.append(msg)

    def load_next() -> None:
        calls["load_next"] += 1

    app._set_status = capture  # type: ignore[method-assign]
    app._load_next = load_next  # type: ignore[method-assign]

    app.action_rate(2)

    assert statuses[-1] == "answer failed: boom"
    assert calls["load_next"] == 0