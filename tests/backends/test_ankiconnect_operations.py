from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from anki_cli.backends.ankiconnect import (
    AnkiConnectAPIError,
    AnkiConnectBackend,
    AnkiConnectProtocolError,
)


@pytest.fixture
def backend() -> Iterator[AnkiConnectBackend]:
    instance = AnkiConnectBackend(verify_version=False)
    try:
        yield instance
    finally:
        instance.close()


def test_add_note_builds_payload_and_normalizes_tags(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_invoke(action: str, **params: Any) -> Any:
        captured["action"] = action
        captured["params"] = params
        return 123

    monkeypatch.setattr(backend, "_invoke", fake_invoke)

    out = backend.add_note(
        deck="Default",
        notetype="Basic",
        fields={"Front": "Q", "Back": "A"},
        tags=["  a ", "b", "a", "", "   "],
        allow_duplicate=True,
    )

    assert out == 123
    assert captured["action"] == "addNote"
    assert captured["params"]["note"] == {
        "deckName": "Default",
        "modelName": "Basic",
        "fields": {"Front": "Q", "Back": "A"},
        "tags": ["a", "b"],
        "options": {"allowDuplicate": True},
    }


def test_add_notes_builds_payload_and_coerces_result(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_invoke(action: str, **params: Any) -> Any:
        captured["action"] = action
        captured["params"] = params
        return [101, None]

    monkeypatch.setattr(backend, "_invoke", fake_invoke)

    out = backend.add_notes(
        [
            {
                "deck": "Default",
                "notetype": "Basic",
                "fields": {"Front": "Q1", "Back": 1},
                "tags": "x, y y",
            },
            {
                "deckName": "Default",
                "modelName": "Basic",
                "fields": {"Front": "Q2", "Back": "A2"},
                "tags": [1, "x"],
            },
        ]
    )

    assert out == [101, None]
    assert captured["action"] == "addNotes"
    assert captured["params"]["notes"] == [
        {
            "deckName": "Default",
            "modelName": "Basic",
            "fields": {"Front": "Q1", "Back": "1"},
            "tags": ["x", "y"],
        },
        {
            "deckName": "Default",
            "modelName": "Basic",
            "fields": {"Front": "Q2", "Back": "A2"},
            "tags": ["1", "x"],
        },
    ]


def test_add_notes_requires_deck_and_notetype(backend: AnkiConnectBackend) -> None:
    with pytest.raises(AnkiConnectProtocolError, match="deck/deckName"):
        backend.add_notes([{"notetype": "Basic", "fields": {"Front": "Q"}}])


def test_add_notes_requires_fields_object(backend: AnkiConnectBackend) -> None:
    with pytest.raises(AnkiConnectProtocolError, match="fields object"):
        backend.add_notes([{"deck": "D", "notetype": "N", "fields": "bad"}])


def test_add_notes_requires_list_result(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "_invoke", lambda action, **params: {"id": 1})

    with pytest.raises(AnkiConnectProtocolError, match="addNotes must return a list"):
        backend.add_notes(
            [{"deck": "Default", "notetype": "Basic", "fields": {"Front": "Q", "Back": "A"}}]
        )


def test_add_notes_item_must_be_int_or_none(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "_invoke", lambda action, **params: ["bad"])

    with pytest.raises(AnkiConnectProtocolError, match="addNotes item must be int"):
        backend.add_notes(
            [{"deck": "Default", "notetype": "Basic", "fields": {"Front": "Q", "Back": "A"}}]
        )


def test_update_note_fields_only(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke(action: str, **params: Any) -> Any:
        calls.append((action, params))
        return None

    monkeypatch.setattr(backend, "_invoke", fake_invoke)

    out = backend.update_note(note_id=7, fields={"Front": "Q2"}, tags=None)

    assert out == {"note_id": 7, "updated_fields": True, "updated_tags": False}
    assert calls == [
        ("updateNoteFields", {"note": {"id": 7, "fields": {"Front": "Q2"}}}),
    ]


def test_update_note_tag_diff_adds_and_removes(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke(action: str, **params: Any) -> Any:
        calls.append((action, params))
        return None

    monkeypatch.setattr(backend, "_invoke", fake_invoke)
    monkeypatch.setattr(backend, "get_note", lambda note_id: {"tags": "a c"})

    out = backend.update_note(note_id=9, fields={"Front": "Q"}, tags=["a", "b"])

    assert out == {"note_id": 9, "updated_fields": True, "updated_tags": True}
    assert calls == [
        ("updateNoteFields", {"note": {"id": 9, "fields": {"Front": "Q"}}}),
        ("addTags", {"notes": [9], "tags": "b"}),
        ("removeTags", {"notes": [9], "tags": "c"}),
    ]


def test_update_note_tags_unchanged_still_marks_updated(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke(action: str, **params: Any) -> Any:
        calls.append((action, params))
        return None

    monkeypatch.setattr(backend, "_invoke", fake_invoke)
    monkeypatch.setattr(backend, "get_note", lambda note_id: {"tags": ["a", "b"]})

    out = backend.update_note(note_id=10, fields=None, tags=["b", "a"])

    assert out == {"note_id": 10, "updated_fields": False, "updated_tags": True}
    assert calls == []


def test_answer_card_validates_ease(backend: AnkiConnectBackend) -> None:
    with pytest.raises(AnkiConnectProtocolError, match="ease must be 1, 2, 3, or 4"):
        backend.answer_card(card_id=1, ease=9)


def test_answer_card_requires_active_gui_card(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "_invoke", lambda action, **params: {})

    with pytest.raises(AnkiConnectAPIError, match="No current GUI card is active"):
        backend.answer_card(card_id=1, ease=3)


def test_answer_card_requires_matching_gui_card(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "_invoke", lambda action, **params: {"cardId": 10})

    with pytest.raises(AnkiConnectAPIError, match="not active in GUI"):
        backend.answer_card(card_id=11, ease=3)


def test_answer_card_fallbacks_to_answer_ease(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke(action: str, **params: Any) -> Any:
        calls.append((action, params))
        if action == "guiCurrentCard":
            return {"cardId": 22}
        if action == "guiAnswerCard" and "ease" in params:
            raise AnkiConnectAPIError("guiAnswerCard", "legacy param required")
        return None

    monkeypatch.setattr(backend, "_invoke", fake_invoke)

    out = backend.answer_card(card_id=22, ease=4)

    assert out == {"card_id": 22, "ease": 4, "answered": True}
    assert calls == [
        ("guiCurrentCard", {}),
        ("guiAnswerCard", {"ease": 4}),
        ("guiAnswerCard", {"answerEase": 4}),
    ]


def test_set_card_flag_range_validation(backend: AnkiConnectBackend) -> None:
    with pytest.raises(ValueError, match=r"range 0..7"):
        backend.set_card_flag([1], -1)
    with pytest.raises(ValueError, match=r"range 0..7"):
        backend.set_card_flag([1], 8)


def test_set_card_flag_success(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke(action: str, **params: Any) -> Any:
        calls.append((action, params))
        return [True]

    monkeypatch.setattr(backend, "_invoke", fake_invoke)

    out = backend.set_card_flag([3, 1, 3], 2)

    assert out == {"updated": 2, "card_ids": [3, 1], "flag": 2}
    assert calls == [
        (
            "setSpecificValueOfCard",
            {"card": 3, "keys": ["flags"], "newValues": [2], "warning_check": True},
        ),
        (
            "setSpecificValueOfCard",
            {"card": 1, "keys": ["flags"], "newValues": [2], "warning_check": True},
        ),
    ]


def test_set_card_flag_protocol_and_api_failures(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "_invoke", lambda action, **params: True)
    with pytest.raises(AnkiConnectProtocolError, match="non-empty list"):
        backend.set_card_flag([1], 1)

    monkeypatch.setattr(backend, "_invoke", lambda action, **params: [False])
    with pytest.raises(AnkiConnectAPIError, match="Failed to set flags"):
        backend.set_card_flag([1], 1)


def test_unbury_cards_all_primary_and_fallback(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke_ok(action: str, **params: Any) -> Any:
        calls.append((action, params))
        return None

    monkeypatch.setattr(backend, "_invoke", fake_invoke_ok)
    out = backend.unbury_cards()
    assert out == {"unburied": True, "scope": "all"}
    assert calls == [("unbury", {})]

    calls.clear()

    def fake_invoke_fallback(action: str, **params: Any) -> Any:
        calls.append((action, params))
        if action == "unbury":
            raise AnkiConnectAPIError("unbury", "unsupported")
        return None

    monkeypatch.setattr(backend, "_invoke", fake_invoke_fallback)
    out2 = backend.unbury_cards()
    assert out2 == {"unburied": True, "scope": "all"}
    assert calls == [("unbury", {}), ("unburyCards", {"cards": []})]


def test_unbury_cards_deck_scope(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "find_cards", lambda query: [])
    out = backend.unbury_cards(deck="DeckA")
    assert out == {"unburied": 0, "deck": "DeckA", "card_ids": []}

    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke(action: str, **params: Any) -> Any:
        calls.append((action, params))
        if action == "unburyCards":
            raise AnkiConnectAPIError("unburyCards", "unsupported")
        return None

    monkeypatch.setattr(backend, "find_cards", lambda query: [7, 8])
    monkeypatch.setattr(backend, "_invoke", fake_invoke)

    out2 = backend.unbury_cards(deck="DeckA")
    assert out2 == {"unburied": 2, "deck": "DeckA", "card_ids": [7, 8]}
    assert calls == [
        ("unburyCards", {"cards": [7, 8]}),
        ("unbury", {}),
    ]


def test_add_remove_tags_and_rename_tag(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke(action: str, **params: Any) -> Any:
        calls.append((action, params))
        return None

    monkeypatch.setattr(backend, "_invoke", fake_invoke)

    out_add = backend.add_tags([2, 1, 2], [" a ", "b", "a"])
    assert out_add == {"updated": 2, "note_ids": [2, 1], "tags": ["a", "b"]}
    assert calls[-1] == ("addTags", {"notes": [2, 1], "tags": "a b"})

    out_remove = backend.remove_tags([2, 1, 2], [" b ", "a", "b"])
    assert out_remove == {"updated": 2, "note_ids": [2, 1], "tags": ["b", "a"]}
    assert calls[-1] == ("removeTags", {"notes": [2, 1], "tags": "b a"})

    monkeypatch.setattr(backend, "find_notes", lambda query: [])
    out_rename_none = backend.rename_tag("x", "y")
    assert out_rename_none == {"from": "x", "to": "y", "updated": 0}

    rename_calls: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke_rename(action: str, **params: Any) -> Any:
        rename_calls.append((action, params))
        return None

    monkeypatch.setattr(backend, "_invoke", fake_invoke_rename)
    monkeypatch.setattr(backend, "find_notes", lambda query: [5, 3])

    out_rename = backend.rename_tag("old", "new")
    assert out_rename == {"from": "old", "to": "new", "updated": 2, "note_ids": [5, 3]}
    assert rename_calls == [
        ("addTags", {"notes": [5, 3], "tags": "new"}),
        ("removeTags", {"notes": [5, 3], "tags": "old"}),
    ]


def test_get_tag_counts_sorts_and_counts(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[str] = []

    monkeypatch.setattr(backend, "get_tags", lambda: ["beta", "Alpha"])

    def fake_find_notes(query: str) -> list[int]:
        queries.append(query)
        return [1] if "Alpha" in query else [1, 2, 3]

    monkeypatch.setattr(backend, "find_notes", fake_find_notes)

    out = backend.get_tag_counts()
    assert out == [{"tag": "Alpha", "count": 1}, {"tag": "beta", "count": 3}]
    assert queries == ['tag:"Alpha"', 'tag:"beta"']


def test_get_note_fields_parses_mapping_values_and_filter(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        backend,
        "get_note",
        lambda note_id: {
            "fields": {
                "Front": {"value": "Q"},
                "Back": {"value": "A"},
                "Plain": "x",
            }
        },
    )

    out_all = backend.get_note_fields(10, None)
    assert out_all == {"Front": "Q", "Back": "A", "Plain": "x"}

    out_subset = backend.get_note_fields(10, [" Front ", "", "Plain", "Missing"])
    assert out_subset == {"Front": "Q", "Plain": "x"}


def test_get_note_fields_requires_object_fields(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "get_note", lambda note_id: {"fields": "not-an-object"})
    with pytest.raises(AnkiConnectProtocolError, match="fields must be an object"):
        backend.get_note_fields(1, None)


def test_get_revlog_not_supported(backend: AnkiConnectBackend) -> None:
    with pytest.raises(NotImplementedError, match="not supported"):
        backend.get_revlog(1, limit=10)