from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import anki_cli.backends.direct as direct_mod
from anki_cli.backends.direct import DirectBackend


@pytest.fixture
def backend_and_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[DirectBackend, MagicMock]:
    db_path = tmp_path / "collection.db"
    db_path.touch()

    store = MagicMock()

    def fake_store(path: Path) -> MagicMock:
        # DirectBackend should pass resolved collection path.
        assert path == db_path.resolve()
        return store

    monkeypatch.setattr(direct_mod, "AnkiDirectReadStore", fake_store)
    backend = DirectBackend(db_path)
    return backend, store


def test_init_missing_collection_raises(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError, match="Direct collection not found"):
        DirectBackend(missing)


def test_init_sets_name_and_resolved_collection_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "collection.db"
    db_path.touch()

    captured: dict[str, Path] = {}
    sentinel_store = object()

    def fake_store(path: Path) -> object:
        captured["path"] = path
        return sentinel_store

    monkeypatch.setattr(direct_mod, "AnkiDirectReadStore", fake_store)

    backend = DirectBackend(db_path)

    assert backend.name == "direct"
    assert backend.collection_path == db_path.resolve()
    assert captured["path"] == db_path.resolve()
    assert backend._store is sentinel_store


@pytest.mark.parametrize(
    (
        "backend_method",
        "store_method",
        "call_args",
        "call_kwargs",
        "expected_args",
        "expected_kwargs",
    ),
    [
        # decks / notetypes
        ("get_decks", "get_decks", (), {}, (), {}),
        ("get_deck", "get_deck", ("Default",), {}, ("Default",), {}),
        ("get_notetypes", "get_notetypes", (), {}, (), {}),
        ("get_notetype", "get_notetype", ("Basic",), {}, ("Basic",), {}),
        (
            "create_notetype",
            "create_notetype",
            (
                "Basic",
                ["Front", "Back"],
                [{"name": "Card 1", "front": "{{Front}}", "back": "{{Back}}"}],
            ),
            {"css": ".card {}", "kind": "normal"},
            (),
            {
                "name": "Basic",
                "fields": ["Front", "Back"],
                "templates": [{"name": "Card 1", "front": "{{Front}}", "back": "{{Back}}"}],
                "css": ".card {}",
                "kind": "normal",
            },
        ),
        (
            "add_notetype_field",
            "add_notetype_field",
            ("Basic", "Hint"),
            {},
            (),
            {"name": "Basic", "field_name": "Hint"},
        ),
        (
            "remove_notetype_field",
            "remove_notetype_field",
            ("Basic", "Hint"),
            {},
            (),
            {"name": "Basic", "field_name": "Hint"},
        ),
        (
            "add_notetype_template",
            "add_notetype_template",
            ("Basic", "Card 2", "{{Back}}", "{{Front}}"),
            {},
            (),
            {
                "name": "Basic",
                "template_name": "Card 2",
                "front": "{{Back}}",
                "back": "{{Front}}",
            },
        ),
        (
            "edit_notetype_template",
            "edit_notetype_template",
            ("Basic", "Card 1"),
            {"front": "Q2", "back": "A2"},
            (),
            {
                "name": "Basic",
                "template_name": "Card 1",
                "front": "Q2",
                "back": "A2",
            },
        ),
        (
            "set_notetype_css",
            "set_notetype_css",
            ("Basic", ".card{color:red}"),
            {},
            (),
            {"name": "Basic", "css": ".card{color:red}"},
        ),
        # notes
        ("find_notes", "find_note_ids", ("tag:foo",), {}, ("tag:foo",), {}),
        ("get_note", "get_note", (123,), {}, (123,), {}),
        (
            "get_note_fields",
            "get_note_fields",
            (123, ["Front"]),
            {},
            (),
            {"note_id": 123, "fields": ["Front"]},
        ),
        # cards
        ("find_cards", "find_card_ids", ("deck:Default",), {}, ("deck:Default",), {}),
        ("get_card", "get_card", (456,), {}, (456,), {}),
        (
            "get_revlog",
            "get_revlog",
            (456,),
            {"limit": 77},
            (),
            {"card_id": 456, "limit": 77},
        ),
        (
            "move_cards",
            "move_cards",
            ([1, 2, 3], "Target"),
            {},
            (),
            {"card_ids": [1, 2, 3], "deck": "Target"},
        ),
        (
            "set_card_flag",
            "set_card_flag",
            ([1, 2], 3),
            {},
            (),
            {"card_ids": [1, 2], "flag": 3},
        ),
        ("bury_cards", "bury_cards", ([1, 2],), {}, (), {"card_ids": [1, 2]}),
        ("unbury_cards", "unbury_cards", (), {"deck": "DeckA"}, (), {"deck": "DeckA"}),
        (
            "reschedule_cards",
            "reschedule_cards",
            ([1, 2], 5),
            {},
            (),
            {"card_ids": [1, 2], "days": 5},
        ),
        ("reset_cards", "reset_cards", ([1, 2],), {}, (), {"card_ids": [1, 2]}),
        # tags / due
        ("get_tags", "get_tags", (), {}, (), {}),
        ("get_due_counts", "get_due_counts", (), {}, (None,), {}),
        ("get_due_counts", "get_due_counts", ("DeckA",), {}, ("DeckA",), {}),
        ("get_tag_counts", "get_tag_counts", (), {}, (), {}),
        (
            "rename_tag",
            "rename_tag",
            ("old", "new"),
            {},
            (),
            {"old_tag": "old", "new_tag": "new"},
        ),
        # write/scheduling operations
        ("create_deck", "create_deck", ("NewDeck",), {}, ("NewDeck",), {}),
        (
            "rename_deck",
            "rename_deck",
            ("Old", "New"),
            {},
            (),
            {"old_name": "Old", "new_name": "New"},
        ),
        ("delete_deck", "delete_deck", ("Trash",), {}, ("Trash",), {}),
        ("get_deck_config", "get_deck_config", ("Default",), {}, ("Default",), {}),
        (
            "set_deck_config",
            "set_deck_config",
            ("Default", {"new_per_day": 30}),
            {},
            (),
            {"name": "Default", "updates": {"new_per_day": 30}},
        ),
        (
            "add_note",
            "add_note",
            ("Default", "Basic", {"Front": "Q", "Back": "A"}),
            {"tags": ["x"], "allow_duplicate": True},
            (),
            {
                "deck": "Default",
                "notetype": "Basic",
                "fields": {"Front": "Q", "Back": "A"},
                "tags": ["x"],
                "allow_duplicate": True,
            },
        ),
        ("add_notes", "add_notes", ([{"deck": "Default"}],), {}, ([{"deck": "Default"}],), {}),
        (
            "update_note",
            "update_note",
            (999,),
            {"fields": {"Front": "Q2"}, "tags": ["x"]},
            (),
            {"note_id": 999, "fields": {"Front": "Q2"}, "tags": ["x"]},
        ),
        ("delete_notes", "delete_notes", ([10, 11],), {}, ([10, 11],), {}),
        ("answer_card", "answer_card", (7, 3), {}, (), {"card_id": 7, "ease": 3}),
        ("suspend_cards", "suspend_cards", ([1, 2],), {}, ([1, 2],), {}),
        ("unsuspend_cards", "unsuspend_cards", ([1, 2],), {}, ([1, 2],), {}),
        ("add_tags", "add_tags", ([100], ["a", "b"]), {}, ([100], ["a", "b"]), {}),
        ("remove_tags", "remove_tags", ([100], ["a"]), {}, ([100], ["a"]), {}),
    ],
)
def test_method_delegation(
    backend_and_store: tuple[DirectBackend, MagicMock],
    backend_method: str,
    store_method: str,
    call_args: tuple[Any, ...],
    call_kwargs: dict[str, Any],
    expected_args: tuple[Any, ...],
    expected_kwargs: dict[str, Any],
) -> None:
    backend, store = backend_and_store
    sentinel = object()

    getattr(store, store_method).return_value = sentinel

    result = getattr(backend, backend_method)(*call_args, **call_kwargs)

    assert result is sentinel
    getattr(store, store_method).assert_called_once_with(*expected_args, **expected_kwargs)