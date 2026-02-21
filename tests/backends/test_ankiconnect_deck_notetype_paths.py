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


def test_get_decks_sorted_and_coerced(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke(action: str, **params: Any) -> Any:
        calls.append((action, params))
        return {"b": 2, "A": 1}

    monkeypatch.setattr(backend, "_invoke", fake_invoke)

    out = backend.get_decks()

    assert out == [{"id": 1, "name": "A"}, {"id": 2, "name": "b"}]
    assert calls == [("deckNamesAndIds", {})]


def test_get_decks_requires_object(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "_invoke", lambda action, **params: ["bad"])

    with pytest.raises(AnkiConnectProtocolError, match="deckNamesAndIds must be an object"):
        backend.get_decks()


def test_get_deck_success(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "get_decks", lambda: [{"id": 7, "name": "Default"}])
    monkeypatch.setattr(
        backend,
        "get_due_counts",
        lambda deck=None: {"new": 1, "learn": 2, "review": 3, "total": 6},
    )

    out = backend.get_deck("  Default  ")

    assert out == {
        "id": 7,
        "name": "Default",
        "due_counts": {"new": 1, "learn": 2, "review": 3, "total": 6},
    }


def test_get_deck_missing_and_invalid_id(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "get_decks", lambda: [])

    with pytest.raises(LookupError, match="Deck not found"):
        backend.get_deck("Default")

    monkeypatch.setattr(backend, "get_decks", lambda: [{"id": "x", "name": "Default"}])

    with pytest.raises(AnkiConnectProtocolError, match="deck id must be int"):
        backend.get_deck("Default")


def test_create_and_delete_deck_invoke_expected_actions(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke(action: str, **params: Any) -> Any:
        calls.append((action, params))
        return None

    monkeypatch.setattr(backend, "_invoke", fake_invoke)

    created = backend.create_deck("NewDeck")
    deleted = backend.delete_deck("NewDeck")

    assert created == {"deck": "NewDeck", "created": True}
    assert deleted == {"deck": "NewDeck", "deleted": True, "cards_deleted": True}
    assert calls == [
        ("createDeck", {"deck": "NewDeck"}),
        ("deleteDecks", {"decks": ["NewDeck"], "cardsToo": True}),
    ]


def test_rename_deck_validates_non_empty_names(backend: AnkiConnectBackend) -> None:
    with pytest.raises(ValueError, match="Deck names cannot be empty"):
        backend.rename_deck(" ", "New")

    with pytest.raises(ValueError, match="Deck names cannot be empty"):
        backend.rename_deck("Old", " ")


def test_rename_deck_primary_strategy_fallback_to_second_signature(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke(action: str, **params: Any) -> Any:
        calls.append((action, params))
        if action == "renameDeck" and params == {"old": "Old", "new": "New"}:
            raise AnkiConnectAPIError("renameDeck", "unsupported")
        return None

    monkeypatch.setattr(backend, "_invoke", fake_invoke)

    out = backend.rename_deck(" Old ", " New ")

    assert out == {"from": "Old", "to": "New", "renamed_decks": 1}
    assert calls[:2] == [
        ("renameDeck", {"old": "Old", "new": "New"}),
        ("renameDeck", {"deck": "Old", "newName": "New"}),
    ]


def test_rename_deck_fallback_subtree_rename_flow(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke(action: str, **params: Any) -> Any:
        calls.append((action, params))
        if action == "renameDeck":
            raise AnkiConnectAPIError("renameDeck", "unsupported")
        return None

    monkeypatch.setattr(backend, "_invoke", fake_invoke)
    monkeypatch.setattr(
        backend,
        "get_decks",
        lambda: [{"name": "Old"}, {"name": "Old::Child"}, {"name": "Else"}],
    )

    queries: list[str] = []

    def fake_find_cards(query: str) -> list[int]:
        queries.append(query)
        if query == 'deck:"Old"':
            return [11]
        if query == 'deck:"Old::Child"':
            return [21, 22]
        return []

    monkeypatch.setattr(backend, "find_cards", fake_find_cards)

    out = backend.rename_deck("Old", "New")

    assert out == {"from": "Old", "to": "New", "renamed_decks": 2, "moved_cards": 3}
    assert queries == ['deck:"Old"', 'deck:"Old::Child"']

    assert [entry for entry in calls if entry[0] == "createDeck"] == [
        ("createDeck", {"deck": "New"}),
        ("createDeck", {"deck": "New::Child"}),
    ]
    assert [entry for entry in calls if entry[0] == "changeDeck"] == [
        ("changeDeck", {"cards": [11], "deck": "New"}),
        ("changeDeck", {"cards": [21, 22], "deck": "New::Child"}),
    ]
    assert [entry for entry in calls if entry[0] == "deleteDecks"] == [
        ("deleteDecks", {"decks": ["Old::Child"], "cardsToo": False}),
        ("deleteDecks", {"decks": ["Old"], "cardsToo": False}),
    ]


def test_rename_deck_fallback_missing_source_raises_lookup_error(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        backend,
        "_invoke",
        lambda action, **params: (
            (_ for _ in ()).throw(AnkiConnectAPIError("renameDeck", "unsupported"))
            if action == "renameDeck"
            else None
        ),
    )
    monkeypatch.setattr(backend, "get_decks", lambda: [{"name": "Else"}])

    with pytest.raises(LookupError, match="Deck not found"):
        backend.rename_deck("Old", "New")


def test_get_deck_config_success(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke(action: str, **params: Any) -> Any:
        calls.append((action, params))
        return {"new_per_day": 30}

    monkeypatch.setattr(backend, "_invoke", fake_invoke)

    out = backend.get_deck_config("  Default  ")

    assert out == {"deck": "Default", "config": {"new_per_day": 30}}
    assert calls == [("getDeckConfig", {"deck": "Default"})]


def test_set_deck_config_no_updates_returns_noop(backend: AnkiConnectBackend) -> None:
    assert backend.set_deck_config(name="Default", updates={}) == {
        "deck": "Default",
        "updated": False,
        "config": {},
    }


def test_set_deck_config_merges_and_saves(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke(action: str, **params: Any) -> Any:
        calls.append((action, params))
        if action == "getDeckConfig":
            return {"new_per_day": 20, "reviews_per_day": 200}
        return None

    monkeypatch.setattr(backend, "_invoke", fake_invoke)

    out = backend.set_deck_config(" Default ", {"new_per_day": 40, "x": "y"})

    assert out == {
        "deck": "Default",
        "updated": True,
        "config": {"new_per_day": 40, "reviews_per_day": 200, "x": "y"},
    }
    assert calls == [
        ("getDeckConfig", {"deck": "Default"}),
        ("saveDeckConfig", {"config": {"new_per_day": 40, "reviews_per_day": 200, "x": "y"}}),
    ]


def test_get_notetypes_success_and_sorting(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_invoke(action: str, **params: Any) -> Any:
        if action == "modelNames":
            return ["cloze", "Basic"]
        if action == "modelFieldNames" and params["modelName"] == "Basic":
            return ["Front", "Back"]
        if action == "modelFieldNames" and params["modelName"] == "cloze":
            return ["Text"]
        if action == "modelTemplates" and params["modelName"] == "Basic":
            return {"Card 2": {}, "card 1": {}}
        if action == "modelTemplates" and params["modelName"] == "cloze":
            return {"Cloze": {}}
        raise AssertionError(f"unexpected action={action} params={params}")

    monkeypatch.setattr(backend, "_invoke", fake_invoke)

    out = backend.get_notetypes()

    assert out == [
        {
            "name": "Basic",
            "field_count": 2,
            "template_count": 2,
            "fields": ["Front", "Back"],
            "templates": ["card 1", "Card 2"],
        },
        {
            "name": "cloze",
            "field_count": 1,
            "template_count": 1,
            "fields": ["Text"],
            "templates": ["Cloze"],
        },
    ]


def test_get_notetypes_requires_model_names_list(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "_invoke", lambda action, **params: {"bad": True})

    with pytest.raises(AnkiConnectProtocolError, match="modelNames must return a list"):
        backend.get_notetypes()


def test_get_notetype_cloze_and_styling_fallback(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_invoke(action: str, **params: Any) -> Any:
        if action == "modelFieldNames":
            return ["Text"]
        if action == "modelTemplates":
            return {"Cloze": {"Front": "{{cloze:Text}}", "Back": "{{cloze:Text}}"}}
        if action == "modelStyling":
            raise AnkiConnectAPIError("modelStyling", "unsupported")
        raise AssertionError(f"unexpected action={action}")

    monkeypatch.setattr(backend, "_invoke", fake_invoke)

    out = backend.get_notetype("Cloze")

    assert out["name"] == "Cloze"
    assert out["fields"] == ["Text"]
    assert out["kind"] == "cloze"
    assert out["styling"] == {}


def test_get_notetype_non_dict_templates_and_styling_dict(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_invoke(action: str, **params: Any) -> Any:
        if action == "modelFieldNames":
            return ["Front"]
        if action == "modelTemplates":
            return ["not-a-dict"]
        if action == "modelStyling":
            return {"css": ".card {}"}
        raise AssertionError(f"unexpected action={action}")

    monkeypatch.setattr(backend, "_invoke", fake_invoke)

    out = backend.get_notetype("Basic")

    assert out["templates"] == {}
    assert out["kind"] == "normal"
    assert out["styling"] == {"css": ".card {}"}


@pytest.mark.parametrize(
    ("name", "fields", "templates", "message"),
    [
        (" ", ["Front"], [{"name": "Card 1", "front": "Q", "back": "A"}], "Notetype name"),
        ("Basic", [], [{"name": "Card 1", "front": "Q", "back": "A"}], "at least one field"),
        ("Basic", ["Front"], [], "At least one template"),
        ("Basic", ["Front"], [{"name": " ", "front": "Q", "back": "A"}], "Template name"),
    ],
)
def test_create_notetype_validations(
    backend: AnkiConnectBackend,
    name: str,
    fields: list[str],
    templates: list[dict[str, str]],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        backend.create_notetype(name=name, fields=fields, templates=templates)


def test_create_notetype_payload(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke(action: str, **params: Any) -> Any:
        calls.append((action, params))
        return None

    monkeypatch.setattr(backend, "_invoke", fake_invoke)

    out = backend.create_notetype(
        name=" Basic ",
        fields=[" Front ", "Back", "  "],
        templates=[{"name": " Card 1 ", "front": "Q", "back": "A"}],
        css=".card{}",
        kind=" ClOzE ",
    )

    assert out == {
        "name": "Basic",
        "created": True,
        "field_count": 2,
        "template_count": 1,
        "kind": "cloze",
    }
    assert calls == [
        (
            "createModel",
            {
                "modelName": "Basic",
                "inOrderFields": ["Front", "Back"],
                "css": ".card{}",
                "isCloze": True,
                "cardTemplates": [{"Name": "Card 1", "Front": "Q", "Back": "A"}],
            },
        )
    ]


def test_field_template_mutator_validation_and_invocations(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="required"):
        backend.add_notetype_field(" ", "X")
    with pytest.raises(ValueError, match="required"):
        backend.remove_notetype_field("Basic", " ")
    with pytest.raises(ValueError, match="required"):
        backend.add_notetype_template(" ", "Card 1", "Q", "A")

    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke(action: str, **params: Any) -> Any:
        calls.append((action, params))
        return None

    monkeypatch.setattr(backend, "_invoke", fake_invoke)

    out_add_field = backend.add_notetype_field(" Basic ", " Hint ")
    out_remove_field = backend.remove_notetype_field("Basic", "Hint")
    out_add_tpl = backend.add_notetype_template("Basic", " Card 2 ", "Q2", "A2")

    assert out_add_field == {"name": "Basic", "field": "Hint", "added": True}
    assert out_remove_field == {"name": "Basic", "field": "Hint", "removed": True}
    assert out_add_tpl == {"name": "Basic", "template": "Card 2", "added": True}
    assert calls == [
        ("modelFieldAdd", {"modelName": "Basic", "fieldName": "Hint"}),
        ("modelFieldRemove", {"modelName": "Basic", "fieldName": "Hint"}),
        (
            "modelTemplateAdd",
            {
                "modelName": "Basic",
                "template": {"Name": "Card 2", "Front": "Q2", "Back": "A2"},
            },
        ),
    ]


def test_edit_notetype_template_validation_missing_and_fallback_update(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="at least one of front/back"):
        backend.edit_notetype_template("Basic", "Card 1")

    monkeypatch.setattr(backend, "_invoke", lambda action, **params: {})
    with pytest.raises(LookupError, match="Template not found"):
        backend.edit_notetype_template("Basic", "Missing", front="Q")

    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke(action: str, **params: Any) -> Any:
        calls.append((action, params))
        if action == "modelTemplates":
            return {"Card 1": {"Front": "Q0", "Back": "A0"}}
        if action == "updateModelTemplates" and "templates" in params:
            raise AnkiConnectAPIError("updateModelTemplates", "legacy shape required")
        return None

    monkeypatch.setattr(backend, "_invoke", fake_invoke)

    out = backend.edit_notetype_template("Basic", "Card 1", front="Q1")

    assert out == {"name": "Basic", "template": "Card 1", "updated": True}
    assert calls == [
        ("modelTemplates", {"modelName": "Basic"}),
        (
            "updateModelTemplates",
            {"model": "Basic", "templates": {"Card 1": {"Front": "Q1", "Back": "A0"}}},
        ),
        (
            "updateModelTemplates",
            {"model": {"name": "Basic", "templates": {"Card 1": {"Front": "Q1", "Back": "A0"}}}},
        ),
    ]


def test_set_notetype_css_fallback(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke(action: str, **params: Any) -> Any:
        calls.append((action, params))
        if action == "updateModelStyling" and isinstance(params.get("model"), str):
            raise AnkiConnectAPIError("updateModelStyling", "legacy shape required")
        return None

    monkeypatch.setattr(backend, "_invoke", fake_invoke)

    out = backend.set_notetype_css(" Basic ", ".card{color:red}")

    assert out == {"name": "Basic", "updated": True, "css": ".card{color:red}"}
    assert calls == [
        ("updateModelStyling", {"model": "Basic", "css": ".card{color:red}"}),
        (
            "updateModelStyling",
            {"model": {"name": "Basic", "css": ".card{color:red}"}},
        ),
    ]


def test_note_and_card_read_delete_wrappers(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert backend.delete_notes([]) == {"deleted": 0}

    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke(action: str, **params: Any) -> Any:
        calls.append((action, params))
        if action == "findNotes":
            return [4, 2]
        if action == "notesInfo":
            if params["notes"] == [1]:
                return [{"id": 1, "fields": {"Front": {"value": "Q"}}}]
            return []
        if action == "findCards":
            return [9]
        if action == "cardsInfo":
            if params["cards"] == [7]:
                return [{"cardId": 7}]
            return []
        return None

    monkeypatch.setattr(backend, "_invoke", fake_invoke)

    assert backend.delete_notes([2, 1, 2]) == {"deleted": 2, "note_ids": [2, 1]}
    assert backend.find_notes("tag:x") == [4, 2]
    assert backend.get_note(1) == {"id": 1, "fields": {"Front": {"value": "Q"}}}
    with pytest.raises(AnkiConnectProtocolError, match="notesInfo returned no rows"):
        backend.get_note(999)

    assert backend.find_cards("deck:Default") == [9]
    assert backend.get_card(7) == {"cardId": 7}
    with pytest.raises(AnkiConnectProtocolError, match="cardsInfo returned no rows"):
        backend.get_card(999)


def test_card_operation_wrappers_and_tag_noops(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert backend.suspend_cards([]) == {"suspended": 0}
    assert backend.unsuspend_cards([]) == {"unsuspended": 0}
    assert backend.move_cards([], "DeckA") == {"moved": 0, "card_ids": []}
    assert backend.bury_cards([]) == {"buried": 0, "card_ids": []}
    assert backend.reschedule_cards([], 3) == {"rescheduled": 0, "card_ids": []}
    assert backend.reset_cards([]) == {"reset": 0, "card_ids": []}
    assert backend.add_tags([], ["x"]) == {"updated": 0}
    assert backend.add_tags([1], []) == {"updated": 0}
    assert backend.remove_tags([], ["x"]) == {"updated": 0}
    assert backend.remove_tags([1], []) == {"updated": 0}

    with pytest.raises(ValueError, match="days must be >= 0"):
        backend.reschedule_cards([1], -1)

    with pytest.raises(ValueError, match="Both tags are required"):
        backend.rename_tag(" ", "new")

    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke(action: str, **params: Any) -> Any:
        calls.append((action, params))
        if action == "getTags":
            return [1, "x"]
        return None

    monkeypatch.setattr(backend, "_invoke", fake_invoke)

    assert backend.suspend_cards([3, 1, 3]) == {"suspended": 2, "card_ids": [3, 1]}
    assert backend.unsuspend_cards([3, 1, 3]) == {"unsuspended": 2, "card_ids": [3, 1]}
    assert backend.move_cards([3, 1, 3], "DeckA") == {
        "moved": 2,
        "card_ids": [3, 1],
        "deck": "DeckA",
    }
    assert backend.bury_cards([3, 1, 3]) == {"buried": 2, "card_ids": [3, 1]}
    assert backend.reschedule_cards([3, 1, 3], 5) == {
        "rescheduled": 2,
        "card_ids": [3, 1],
        "days": 5,
    }
    assert backend.reset_cards([3, 1, 3]) == {"reset": 2, "card_ids": [3, 1]}
    assert backend.get_tags() == ["1", "x"]


def test_helper_edge_cases_for_validate_and_tag_coercion(
    backend: AnkiConnectBackend,
) -> None:
    with pytest.raises(AnkiConnectProtocolError, match="host and port"):
        backend._validate_url(url="http:///nohost", allow_non_localhost=True)

    with pytest.raises(AnkiConnectProtocolError, match="host is invalid"):
        backend._validate_url(url="http://:8765", allow_non_localhost=True)

    with pytest.raises(AnkiConnectProtocolError, match=r"ids must be a list|must be a list"):
        backend._as_int_list("bad", "ids")

    with pytest.raises(AnkiConnectProtocolError, match="ids items must be int"):
        backend._as_int_list([1, "x"], "ids")

    assert backend._as_str_list([1, "x"], "tags") == ["1", "x"]
    with pytest.raises(AnkiConnectProtocolError, match="tags must be a list"):
        backend._as_str_list({"k": "v"}, "tags")

    assert backend._coerce_tag_input(None) == []
    assert backend._coerce_tag_input(["a", 1]) == ["a", "1"]
    assert backend._coerce_tag_input("a, b  c") == ["a", "b", "c"]
    with pytest.raises(AnkiConnectProtocolError, match="tags must be list"):
        backend._coerce_tag_input(123)

    assert backend._extract_tags(None) == []
    assert backend._extract_tags(["a", 1]) == ["a", "1"]
    assert backend._extract_tags(" a, b ") == ["a", "b"]
    assert backend._extract_tags("") == []
    assert backend._extract_tags(123) == []