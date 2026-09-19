from __future__ import annotations

from collections.abc import Callable, Iterator
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


def _server(handle: Callable[[str, dict[str, Any]], Any]) -> Callable[..., Any]:
    """An ``_invoke`` stand-in that behaves like AnkiConnect for ``multi``:
    each sub-action is dispatched to ``handle`` and answered with its own
    ``{"result", "error"}`` envelope, an ``AnkiConnectAPIError`` becoming the
    ``error`` text rather than escaping the batch."""

    def invoke(action: str, **params: Any) -> Any:
        if action != "multi":
            return handle(action, params)
        out = []
        for sub in params["actions"]:
            assert sub["version"] == 6, sub
            try:
                out.append({"result": handle(sub["action"], sub.get("params", {})), "error": None})
            except AnkiConnectAPIError as exc:
                out.append({"result": None, "error": exc.api_message})
        return out

    return invoke


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

    # AnkiConnect cannot tell normal from filtered; the canonical shape says so.
    assert out == [
        {"id": 1, "name": "A", "kind": "unknown"},
        {"id": 2, "name": "b", "kind": "unknown"},
    ]
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
        "kind": "unknown",
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


def test_rename_deck_emulates_via_create_move_delete(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AnkiConnect has no rename action (v6 API), so there is nothing to try
    first: the emulation *is* the implementation (#32)."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke(action: str, **params: Any) -> Any:
        calls.append((action, params))
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

    out = backend.rename_deck(" Old ", " New ")

    assert out == {"from": "Old", "to": "New", "renamed_decks": 2, "moved_cards": 3}
    assert queries == ['deck:"Old"', 'deck:"Old::Child"']
    assert not any(entry[0] == "renameDeck" for entry in calls)
    assert calls == [
        ("createDeck", {"deck": "New"}),
        ("createDeck", {"deck": "New::Child"}),
        ("changeDeck", {"cards": [11], "deck": "New"}),
        ("changeDeck", {"cards": [21, 22], "deck": "New::Child"}),
        ("deleteDecks", {"decks": ["Old::Child"], "cardsToo": False}),
        ("deleteDecks", {"decks": ["Old"], "cardsToo": False}),
    ]


def test_rename_deck_missing_source_raises_lookup_error(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "_invoke", lambda action, **params: pytest.fail(action))
    monkeypatch.setattr(backend, "get_decks", lambda: [{"name": "Else"}])

    with pytest.raises(LookupError, match="Deck not found"):
        backend.rename_deck("Old", "New")


@pytest.mark.parametrize("target", ["Taken", "Taken::Sub"])
def test_rename_deck_refuses_an_occupied_target_before_touching_anything(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    """The emulation is not atomic; refusing up front is what stops a partial
    run from merging Old's cards into an unrelated deck. Same error as the
    direct backend."""
    monkeypatch.setattr(backend, "_invoke", lambda action, **params: pytest.fail(action))
    monkeypatch.setattr(
        backend, "get_decks", lambda: [{"name": "Old"}, {"name": "Taken"}, {"name": "Taken::Sub"}]
    )

    with pytest.raises(ValueError, match="Target deck path already exists"):
        backend.rename_deck("Old", target)


def test_rename_deck_into_its_own_subtree_is_refused(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Old -> Old::New`` would create ``Old::New`` and then delete ``Old`` —
    taking the new subtree with it. Refuse before any request."""
    monkeypatch.setattr(backend, "_invoke", lambda action, **params: pytest.fail(action))
    monkeypatch.setattr(backend, "get_decks", lambda: [{"name": "Old"}, {"name": "Old::Child"}])

    with pytest.raises(ValueError, match="into its own subtree"):
        backend.rename_deck("Old", "Old::New")


def test_rename_deck_onto_an_ancestor_is_an_occupied_target(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "_invoke", lambda action, **params: pytest.fail(action))
    monkeypatch.setattr(backend, "get_decks", lambda: [{"name": "A"}, {"name": "A::B"}])

    with pytest.raises(ValueError, match="Target deck path already exists"):
        backend.rename_deck("A::B", "A")


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
    def handle(action: str, params: dict[str, Any]) -> Any:
        if action == "modelNamesAndIds":
            return {"cloze": 200, "Basic": 100}
        if action == "modelFieldNames" and params["modelName"] == "Basic":
            return ["Front", "Back"]
        if action == "modelFieldNames" and params["modelName"] == "cloze":
            return ["Text"]
        if action == "modelTemplates" and params["modelName"] == "Basic":
            return {"Card 2": {}, "card 1": {}}
        if action == "modelTemplates" and params["modelName"] == "cloze":
            return {"Cloze": {}}
        raise AssertionError(f"unexpected action={action} params={params}")

    top_level: list[str] = []
    server = _server(handle)

    def counting_invoke(action: str, **params: Any) -> Any:
        top_level.append(action)
        return server(action, **params)

    monkeypatch.setattr(backend, "_invoke", counting_invoke)

    out = backend.get_notetypes()

    # Two round trips for any number of models (was 1 + 2 per model, #32).
    assert top_level == ["modelNamesAndIds", "multi"]
    assert out == [
        {
            "id": 100,
            "name": "Basic",
            "field_count": 2,
            "template_count": 2,
            "fields": ["Front", "Back"],
            "templates": ["card 1", "Card 2"],
        },
        {
            "id": 200,
            "name": "cloze",
            "field_count": 1,
            "template_count": 1,
            "fields": ["Text"],
            "templates": ["Cloze"],
        },
    ]


def test_get_notetypes_requires_model_map(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "_invoke", lambda action, **params: ["bad"])

    with pytest.raises(AnkiConnectProtocolError, match="modelNamesAndIds must be an object"):
        backend.get_notetypes()


def test_get_notetypes_api_error_propagates(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``modelNamesAndIds`` is part of the v6 API ``check_version`` pins, so an
    error from it is a real error, not a cue to degrade to ``modelNames``."""

    def handle(action: str, params: dict[str, Any]) -> Any:
        raise AnkiConnectAPIError(action, "collection is not open")

    monkeypatch.setattr(backend, "_invoke", _server(handle))

    with pytest.raises(AnkiConnectAPIError, match="collection is not open"):
        backend.get_notetypes()


def test_get_notetypes_with_no_models_makes_one_request(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_invoke(action: str, **params: Any) -> Any:
        calls.append(action)
        return {}

    monkeypatch.setattr(backend, "_invoke", fake_invoke)

    assert backend.get_notetypes() == []
    assert calls == ["modelNamesAndIds"]  # no empty multi


def test_get_notetype_is_one_multi_and_detects_cloze(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handle(action: str, params: dict[str, Any]) -> Any:
        if action == "modelFieldNames":
            return ["Text"]
        if action == "modelTemplates":
            return {"Cloze": {"Front": "{{cloze:Text}}", "Back": "{{cloze:Text}}"}}
        if action == "modelStyling":
            return {"css": ".cloze {}"}
        if action == "modelNamesAndIds":
            return {"Cloze": 55}
        raise AssertionError(f"unexpected action={action}")

    top_level: list[tuple[str, list[str]]] = []
    server = _server(handle)

    def counting_invoke(action: str, **params: Any) -> Any:
        subs = [a["action"] for a in params["actions"]] if action == "multi" else []
        top_level.append((action, subs))
        return server(action, **params)

    monkeypatch.setattr(backend, "_invoke", counting_invoke)

    out = backend.get_notetype("Cloze")

    # One round trip (was 4 sequential requests, #32).
    assert top_level == [
        ("multi", ["modelFieldNames", "modelTemplates", "modelStyling", "modelNamesAndIds"])
    ]
    assert out["id"] == 55
    assert out["name"] == "Cloze"
    assert out["fields"] == ["Text"]
    assert out["kind"] == "cloze"
    assert out["styling"] == {"css": ".cloze {}"}
    # Templates carry the direct backend's ``ord`` (insertion order on AnkiConnect).
    assert out["templates"] == {
        "Cloze": {"Front": "{{cloze:Text}}", "Back": "{{cloze:Text}}", "ord": 0}
    }


def test_get_notetype_non_dict_templates_and_styling_dict(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handle(action: str, params: dict[str, Any]) -> Any:
        if action == "modelFieldNames":
            return ["Front"]
        if action == "modelTemplates":
            return ["not-a-dict"]
        if action == "modelStyling":
            return "not-a-dict-either"
        if action == "modelNamesAndIds":
            return {"Other": 1}  # this model is not in the map
        raise AssertionError(f"unexpected action={action}")

    monkeypatch.setattr(backend, "_invoke", _server(handle))

    out = backend.get_notetype("Basic")

    assert out["templates"] == {}
    assert out["kind"] == "normal"
    assert out["styling"] == {}
    assert out["id"] is None  # unknown to the id map; the rest still works


def test_get_notetype_sub_action_error_names_the_action(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handle(action: str, params: dict[str, Any]) -> Any:
        if action == "modelTemplates":
            raise AnkiConnectAPIError(action, "model was not found: Nope")
        return {} if action != "modelFieldNames" else []

    monkeypatch.setattr(backend, "_invoke", _server(handle))

    with pytest.raises(AnkiConnectAPIError) as excinfo:
        backend.get_notetype("Nope")
    assert excinfo.value.action == "modelTemplates"


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


def test_edit_notetype_template_validation_missing_and_documented_update(
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
        return None

    monkeypatch.setattr(backend, "_invoke", fake_invoke)

    out = backend.edit_notetype_template("Basic", "Card 1", front="Q1")

    assert out == {"name": "Basic", "template": "Card 1", "updated": True}
    # The v6 shape is ``model: {name, templates}``; the old first attempt with
    # ``model=<name>, templates=...`` always failed and hid real errors (#32).
    assert calls == [
        ("modelTemplates", {"modelName": "Basic"}),
        (
            "updateModelTemplates",
            {"model": {"name": "Basic", "templates": {"Card 1": {"Front": "Q1", "Back": "A0"}}}},
        ),
    ]


def test_edit_notetype_template_api_error_is_not_swallowed(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_invoke(action: str, **params: Any) -> Any:
        if action == "modelTemplates":
            return {"Card 1": {"Front": "Q0", "Back": "A0"}}
        raise AnkiConnectAPIError(action, "model was not found: Basic")

    monkeypatch.setattr(backend, "_invoke", fake_invoke)

    with pytest.raises(AnkiConnectAPIError, match="model was not found"):
        backend.edit_notetype_template("Basic", "Card 1", front="Q1")


def test_set_notetype_css_sends_the_documented_shape_once(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke(action: str, **params: Any) -> Any:
        calls.append((action, params))
        return None

    monkeypatch.setattr(backend, "_invoke", fake_invoke)

    out = backend.set_notetype_css(" Basic ", ".card{color:red}")

    assert out == {"name": "Basic", "updated": True, "css": ".card{color:red}"}
    assert calls == [
        ("updateModelStyling", {"model": {"name": "Basic", "css": ".card{color:red}"}}),
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
                return [{"noteId": 1, "modelName": "Basic", "tags": ["t"], "mod": 5,
                         "fields": {"Back": {"value": "A", "order": 1},
                                    "Front": {"value": "Q", "order": 0}}}]
            if params["notes"] == [998]:
                return [{}]
            return []
        if action == "findCards":
            return [9]
        if action == "cardsInfo":
            if params["cards"] == [998]:
                return [{}]
            if params["cards"] == [7]:
                return [{"cardId": 7, "note": 1, "deckName": "D", "modelName": "Basic",
                         "type": 2, "queue": 2, "due": 40, "interval": 3, "factor": 2500,
                         "reps": 4, "lapses": 0, "left": 0, "ord": 0,
                         "fields": {"Front": {"value": "Q", "order": 0}}}]
            return []
        return None

    monkeypatch.setattr(backend, "_invoke", fake_invoke)

    assert backend.delete_notes([2, 1, 2]) == {"deleted": 2, "note_ids": [2, 1]}
    assert backend.find_notes("tag:x") == [4, 2]
    note = backend.get_note(1)
    # Canonical keys added; AnkiConnect's own kept.
    assert note["id"] == 1 and note["noteId"] == 1
    assert note["notetype_name"] == "Basic" and note["modelName"] == "Basic"
    assert note["fields"] == ["Q", "A"]  # notetype order, by ``order``
    assert note["field_names"] == ["Front", "Back"]
    assert note["tags"] == ["t"] and note["mod"] == 5
    with pytest.raises(AnkiConnectProtocolError, match="notesInfo returned no rows"):
        backend.get_note(999)
    # AnkiConnect answers an unknown id with {} (not an error); that must be
    # ENTITY_NOT_FOUND like the direct backend, not a note with id 0.
    with pytest.raises(LookupError, match="Note not found: 998"):
        backend.get_note(998)

    assert backend.find_cards("deck:Default") == [9]
    card = backend.get_card(7)
    assert card["cardId"] == 7 and card["note"] == 1
    assert card["notetype_name"] == "Basic" and card["modelName"] == "Basic"
    assert card["fields"] == ["Q"] and card["field_names"] == ["Front"]
    assert card["due_info"] == {"kind": "review_day_index", "raw": 40, "day_index": 40}
    assert card["left_info"] == {"raw": 0, "today_remaining": 0, "until_graduation": 0}
    assert card["tags"] == []  # cardsInfo has no tags; canonical key still present
    with pytest.raises(AnkiConnectProtocolError, match="cardsInfo returned no rows"):
        backend.get_card(999)
    with pytest.raises(LookupError, match="Card not found: 998"):
        backend.get_card(998)


def test_card_operation_wrappers_and_tag_noops(
    backend: AnkiConnectBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert backend.suspend_cards([]) == {"suspended": 0}
    assert backend.unsuspend_cards([]) == {"unsuspended": 0}
    assert backend.move_cards([], "DeckA") == {"moved": 0, "card_ids": []}
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
