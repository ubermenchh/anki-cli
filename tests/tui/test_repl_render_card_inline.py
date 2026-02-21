from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("prompt_toolkit")
pytest.importorskip("markdownify")

import anki_cli.tui.repl as repl_mod

pytestmark = pytest.mark.tui


def test_render_card_inline_returns_none_when_note_id_missing() -> None:
    class Backend:
        def get_card(self, card_id: int) -> dict[str, Any]:
            return {"id": card_id, "ord": 0}

    assert repl_mod._render_card_inline(Backend(), 1) is None


def test_render_card_inline_uses_notetype_name_from_card() -> None:
    calls = {"get_note_called": False}

    class Backend:
        def get_card(self, card_id: int) -> dict[str, Any]:
            return {"id": card_id, "note": 10, "ord": 0, "notetype_name": "Basic"}

        def get_note_fields(self, note_id: int, fields: list[str] | None = None) -> dict[str, str]:
            assert note_id == 10
            return {"Front": "Q", "Back": "A"}

        def get_note(self, note_id: int) -> dict[str, Any]:
            calls["get_note_called"] = True
            return {"modelName": "ShouldNotBeUsed"}

        def get_notetype(self, name: str) -> dict[str, Any]:
            assert name == "Basic"
            return {
                "kind": "normal",
                "templates": {"Card 1": {"Front": "{{Front}}", "Back": "{{Back}}"}},
            }

    rendered = repl_mod._render_card_inline(Backend(), 1)

    assert rendered == ("Q", "A")
    assert calls["get_note_called"] is False


def test_render_card_inline_falls_back_to_note_model_name() -> None:
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

    rendered = repl_mod._render_card_inline(Backend(), 2)

    assert rendered == ("Hello", "World")


def test_render_card_inline_returns_none_when_notetype_unresolved() -> None:
    class Backend:
        def get_card(self, card_id: int) -> dict[str, Any]:
            return {"id": card_id, "note": 30, "ord": 0}

        def get_note_fields(self, note_id: int, fields: list[str] | None = None) -> dict[str, str]:
            return {"Front": "Q", "Back": "A"}

        def get_note(self, note_id: int) -> dict[str, Any]:
            return {}  # no modelName

    assert repl_mod._render_card_inline(Backend(), 3) is None


def test_render_card_inline_prefers_template_matching_ord_field() -> None:
    class Backend:
        def get_card(self, card_id: int) -> dict[str, Any]:
            return {"id": card_id, "note": 40, "ord": 1, "notetype_name": "Basic"}

        def get_note_fields(self, note_id: int, fields: list[str] | None = None) -> dict[str, str]:
            return {"Front": "Q", "Back": "A"}

        def get_note(self, note_id: int) -> dict[str, Any]:
            return {"modelName": "Basic"}

        def get_notetype(self, name: str) -> dict[str, Any]:
            return {
                "kind": "normal",
                "templates": {
                    "Card A": {"ord": 0, "Front": "F0 {{Front}}", "Back": "{{Back}}"},
                    "Card B": {"ord": 1, "Front": "F1 {{Front}}", "Back": "{{Back}}"},
                },
            }

    rendered = repl_mod._render_card_inline(Backend(), 4)

    assert rendered is not None
    question, answer = rendered
    assert question == "F1 Q"
    assert answer == "A"


def test_render_card_inline_falls_back_to_template_index_when_no_ord_fields() -> None:
    class Backend:
        def get_card(self, card_id: int) -> dict[str, Any]:
            return {"id": card_id, "note": 50, "ord": 1, "notetype_name": "Basic"}

        def get_note_fields(self, note_id: int, fields: list[str] | None = None) -> dict[str, str]:
            return {"Front": "Q", "Back": "A"}

        def get_note(self, note_id: int) -> dict[str, Any]:
            return {"modelName": "Basic"}

        def get_notetype(self, name: str) -> dict[str, Any]:
            return {
                "kind": "normal",
                "templates": {
                    "Card 1": {"Front": "T1 {{Front}}", "Back": "{{Back}}"},
                    "Card 2": {"Front": "T2 {{Front}}", "Back": "{{Back}}"},
                },
            }

    rendered = repl_mod._render_card_inline(Backend(), 5)

    assert rendered is not None
    question, answer = rendered
    assert question == "T2 Q"
    assert answer == "A"


def test_render_card_inline_returns_none_when_no_templates_exist() -> None:
    class Backend:
        def get_card(self, card_id: int) -> dict[str, Any]:
            return {"id": card_id, "note": 60, "ord": 0, "notetype_name": "Basic"}

        def get_note_fields(self, note_id: int, fields: list[str] | None = None) -> dict[str, str]:
            return {"Front": "Q", "Back": "A"}

        def get_note(self, note_id: int) -> dict[str, Any]:
            return {"modelName": "Basic"}

        def get_notetype(self, name: str) -> dict[str, Any]:
            return {"kind": "normal", "templates": {}}

    assert repl_mod._render_card_inline(Backend(), 6) is None


def test_render_card_inline_cloze_question_and_answer() -> None:
    class Backend:
        def get_card(self, card_id: int) -> dict[str, Any]:
            return {"id": card_id, "note": 70, "ord": 0, "notetype_name": "Cloze"}

        def get_note_fields(self, note_id: int, fields: list[str] | None = None) -> dict[str, str]:
            return {"Text": "{{c1::Paris}} is in {{c2::France}}"}

        def get_note(self, note_id: int) -> dict[str, Any]:
            return {"modelName": "Cloze"}

        def get_notetype(self, name: str) -> dict[str, Any]:
            return {
                "kind": "cloze",
                "templates": {
                    "Cloze": {"Front": "{{cloze:Text}}", "Back": "{{cloze:Text}}"},
                },
            }

    rendered = repl_mod._render_card_inline(Backend(), 7)

    assert rendered == ("[...] is in France", "Paris is in France")