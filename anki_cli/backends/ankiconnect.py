from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import httpx

from anki_cli.backends.protocol import AnkiBackend, JSONValue

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

class AnkiConnectError(RuntimeError):
    """Base error for AnkiConnect backend."""


class AnkiConnectUnavailableError(AnkiConnectError):
    """Raised when the local AnkiConnect service cannot be reached."""


class AnkiConnectProtocolError(AnkiConnectError):
    """Raised when AnkiConnect returns malformed payloads."""


class AnkiConnectAPIError(AnkiConnectError):
    """Raised when AnkiConnect returns an explicit API error."""

    def __init__(self, action: str, message: str) -> None:
        super().__init__(f"AnkiConnect action '{action}' failed: {message}")
        self.action = action
        self.api_message = message


class AnkiConnectBackend(AnkiBackend):
    name = "ankiconnect"

    def __init__(
        self,
        *,
        url: str = "http://localhost:8765",
        timeout_seconds: float = 2.0,
        api_version: int = 6,
        verify_version: bool = True,
        allow_non_localhost: bool = False,
        collection_path: Path | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.collection_path = collection_path
        self._url = url
        self._api_version = api_version
        self._timeout_seconds = timeout_seconds
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)

        self._validate_url(url=url, allow_non_localhost=allow_non_localhost)

        if verify_version:
            try:
                self.check_version()
            except Exception:
                self.close()
                raise

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> AnkiConnectBackend:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def check_version(self) -> int:
        result = self._invoke("version")
        if not isinstance(result, int):
            raise AnkiConnectProtocolError(
                f"Expected integer from version action, got {type(result).__name__}."
            )
        if result < self._api_version:
            raise AnkiConnectProtocolError(
                f"AnkiConnect version {result} is too old. Require >= {self._api_version}."
            )
        return result

    def _invoke(self, action: str, **params: JSONValue) -> JSONValue:
        payload: dict[str, Any] = {
            "action": action,
            "version": self._api_version,
            "params": params,
        }

        try:
            response = self._client.post(
                self._url,
                json=payload,
                headers={"Connection": "close"},
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise AnkiConnectUnavailableError(
                f"Timeout contacting AnkiConnect at {self._url}."
            ) from exc
        except httpx.ConnectError as exc:
            raise AnkiConnectUnavailableError(
                f"Cannot connect to AnkiConnect at {self._url}. Is Anki running with the add-on?"
            ) from exc
        except httpx.RemoteProtocolError as exc:
            raise AnkiConnectUnavailableError(
                f"AnkiConnect at {self._url} disconnected unexpectedly: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise AnkiConnectUnavailableError(
                f"HTTP error contacting AnkiConnect at {self._url}: {exc}"
            ) from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise AnkiConnectProtocolError("AnkiConnect returned non-JSON response.") from exc

        if not isinstance(body, dict):
            raise AnkiConnectProtocolError(
                f"AnkiConnect response must be an object, got {type(body).__name__}."
            )
        if "error" not in body or "result" not in body:
            raise AnkiConnectProtocolError(
                "AnkiConnect response missing required keys: 'error' and 'result'."
            )

        error = body["error"]
        if error is not None:
            raise AnkiConnectAPIError(action, str(error))

        return body["result"]

    # Decks
    def get_decks(self) -> list[dict[str, JSONValue]]:
        result = self._invoke("deckNamesAndIds")
        deck_map = self._as_json_object(result, "deckNamesAndIds")

        decks: list[dict[str, JSONValue]] = []
        for name, deck_id in sorted(deck_map.items(), key=lambda item: str(item[0]).lower()):
            decks.append(
                {
                    "id": self._as_int(deck_id, "deck id"),
                    "name": str(name),
                }
            )
        return decks

    def get_deck(self, name: str) -> dict[str, JSONValue]:
        normalized = name.strip()
        decks = self.get_decks()
        match = next((item for item in decks if str(item.get("name", "")) == normalized), None)
        if match is None:
            raise LookupError(f"Deck not found: {normalized}")

        deck_id = match.get("id")
        if not isinstance(deck_id, int):
            raise AnkiConnectProtocolError("deck id must be int.")
        return {
            "id": deck_id,
            "name": normalized,
            "due_counts": self.get_due_counts(deck=normalized),
        }

    def create_deck(self, name: str) -> dict[str, JSONValue]:
        self._invoke("createDeck", deck=name)
        return {"deck": name, "created": True}

    def rename_deck(self, old_name: str, new_name: str) -> dict[str, JSONValue]:
        source = old_name.strip()
        target = new_name.strip()
        if not source or not target:
            raise ValueError("Deck names cannot be empty.")

        for params in (
            {"old": source, "new": target},
            {"deck": source, "newName": target},
            {"deck": source, "name": target},
        ):
            try:
                self._invoke("renameDeck", **params)
                return {"from": source, "to": target, "renamed_decks": 1}
            except AnkiConnectAPIError:
                continue

        decks = [str(item["name"]) for item in self.get_decks()]
        targets = sorted(
            [name for name in decks if name == source or name.startswith(f"{source}::")],
            key=lambda value: value.count("::"),
        )
        if not targets:
            raise LookupError(f"Deck not found: {source}")

        rename_map: dict[str, str] = {}
        for deck_name in targets:
            suffix = deck_name[len(source) :]
            rename_map[deck_name] = f"{target}{suffix}"

        for next_name in rename_map.values():
            self._invoke("createDeck", deck=next_name)

        moved_cards = 0
        for from_name, to_name in rename_map.items():
            card_ids = self.find_cards(f'deck:"{from_name}"')
            if card_ids:
                self._invoke("changeDeck", cards=card_ids, deck=to_name)
                moved_cards += len(card_ids)

        for old_deck in sorted(
            rename_map.keys(),
            key=lambda value: value.count("::"),
            reverse=True,
        ):
            self._invoke("deleteDecks", decks=[old_deck], cardsToo=False)

        return {
            "from": source,
            "to": target,
            "renamed_decks": len(rename_map),
            "moved_cards": moved_cards,
        }

    def delete_deck(self, name: str) -> dict[str, JSONValue]:
        self._invoke("deleteDecks", decks=[name], cardsToo=True)
        return {"deck": name, "deleted": True, "cards_deleted": True}

    def get_deck_config(self, name: str) -> dict[str, JSONValue]:
        normalized = name.strip()
        result = self._invoke("getDeckConfig", deck=normalized)
        config = self._as_json_object(result, "getDeckConfig")
        return {"deck": normalized, "config": config}

    def set_deck_config(
        self,
        name: str,
        updates: dict[str, JSONValue],
    ) -> dict[str, JSONValue]:
        normalized = name.strip()
        if not updates:
            return {"deck": normalized, "updated": False, "config": {}}

        raw_config = self._invoke("getDeckConfig", deck=normalized)
        config = self._as_json_object(raw_config, "getDeckConfig")
        for key, value in updates.items():
            config[key] = value
        self._invoke("saveDeckConfig", config=config)
        return {"deck": normalized, "updated": True, "config": config}

    # Notetypes
    def get_notetypes(self) -> list[dict[str, JSONValue]]:
        result = self._invoke("modelNames")
        if not isinstance(result, list):
            raise AnkiConnectProtocolError("modelNames must return a list.")

        names = [str(item) for item in result]
        output: list[dict[str, JSONValue]] = []

        for name in sorted(names, key=str.lower):
            fields_raw = self._invoke("modelFieldNames", modelName=name)
            templates_raw = self._invoke("modelTemplates", modelName=name)

            fields = self._as_str_list(fields_raw, "modelFieldNames")
            template_names = (
                sorted([str(k) for k in templates_raw], key=str.lower)
                if isinstance(templates_raw, dict)
                else []
            )

            output.append(
                {
                    "name": name,
                    "field_count": len(fields),
                    "template_count": len(template_names),
                    "fields": fields,
                    "templates": template_names,
                }
            )

        return output

    def get_notetype(self, name: str) -> dict[str, JSONValue]:
        fields_raw = self._invoke("modelFieldNames", modelName=name)
        templates_raw = self._invoke("modelTemplates", modelName=name)

        result: dict[str, JSONValue] = {
            "name": name,
            "fields": self._as_str_list(fields_raw, "modelFieldNames"),
            "templates": templates_raw if isinstance(templates_raw, dict) else {},
        }

        kind = "normal"
        if isinstance(templates_raw, Mapping):
            templates_map = cast(Mapping[str, JSONValue], templates_raw)
            for tmpl in templates_map.values():
                if isinstance(tmpl, Mapping):
                    tmpl_map = cast(Mapping[str, JSONValue], tmpl)
                    front = str(tmpl_map.get("Front") or "")
                    back = str(tmpl_map.get("Back") or "")
                    if "{{cloze:" in front or "{{cloze:" in back:
                        kind = "cloze"
                        break
                    
        result["kind"] = kind

        try:
            styling = self._invoke("modelStyling", modelName=name)
            if isinstance(styling, dict):
                result["styling"] = styling
        except AnkiConnectAPIError:
            # Not all AnkiConnect versions expose modelStyling.
            result["styling"] = {}

        return result

    def create_notetype(
        self,
        name: str,
        fields: list[str],
        templates: list[dict[str, str]],
        *,
        css: str = "",
        kind: str = "normal",
    ) -> dict[str, JSONValue]:
        model_name = name.strip()
        cleaned_fields = [field.strip() for field in fields if field.strip()]
        if not model_name or not cleaned_fields:
            raise ValueError("Notetype name and at least one field are required.")
        if not templates:
            raise ValueError("At least one template is required.")

        template_payload: list[dict[str, str]] = []
        for template in templates:
            tname = str(template.get("name", "")).strip()
            if not tname:
                raise ValueError("Template name cannot be empty.")
            template_payload.append(
                {
                    "Name": tname,
                    "Front": str(template.get("front", "")),
                    "Back": str(template.get("back", "")),
                }
            )

        self._invoke(
            "createModel",
            modelName=model_name,
            inOrderFields=cleaned_fields,
            css=css,
            isCloze=(kind.strip().lower() == "cloze"),
            cardTemplates=template_payload,
        )
        return {
            "name": model_name,
            "created": True,
            "field_count": len(cleaned_fields),
            "template_count": len(template_payload),
            "kind": kind.strip().lower(),
        }

    def add_notetype_field(self, name: str, field_name: str) -> dict[str, JSONValue]:
        model_name = name.strip()
        normalized = field_name.strip()
        if not model_name or not normalized:
            raise ValueError("Notetype and field name are required.")
        self._invoke("modelFieldAdd", modelName=model_name, fieldName=normalized)
        return {"name": model_name, "field": normalized, "added": True}

    def remove_notetype_field(self, name: str, field_name: str) -> dict[str, JSONValue]:
        model_name = name.strip()
        normalized = field_name.strip()
        if not model_name or not normalized:
            raise ValueError("Notetype and field name are required.")
        self._invoke("modelFieldRemove", modelName=model_name, fieldName=normalized)
        return {"name": model_name, "field": normalized, "removed": True}

    def add_notetype_template(
        self,
        name: str,
        template_name: str,
        front: str,
        back: str,
    ) -> dict[str, JSONValue]:
        model_name = name.strip()
        normalized = template_name.strip()
        if not model_name or not normalized:
            raise ValueError("Notetype and template name are required.")
        self._invoke(
            "modelTemplateAdd",
            modelName=model_name,
            template={"Name": normalized, "Front": front, "Back": back},
        )
        return {"name": model_name, "template": normalized, "added": True}

    def edit_notetype_template(
        self,
        name: str,
        template_name: str,
        *,
        front: str | None = None,
        back: str | None = None,
    ) -> dict[str, JSONValue]:
        model_name = name.strip()
        normalized = template_name.strip()
        if not model_name or not normalized:
            raise ValueError("Notetype and template name are required.")
        if front is None and back is None:
            raise ValueError("Provide at least one of front/back.")

        raw_templates = self._invoke("modelTemplates", modelName=model_name)
        templates = self._as_json_object(raw_templates, "modelTemplates")
        existing = templates.get(normalized)
        if not isinstance(existing, Mapping):
            raise LookupError(f"Template not found: {normalized}")

        existing_map = cast(Mapping[str, JSONValue], existing)
        current_front = str(existing_map.get("Front") or "")
        current_back = str(existing_map.get("Back") or "")
        updates = {
            normalized: {
                "Front": front if front is not None else current_front,
                "Back": back if back is not None else current_back,
            }
        }
        try:
            self._invoke("updateModelTemplates", model=model_name, templates=updates)
        except AnkiConnectAPIError:
            self._invoke(
                "updateModelTemplates",
                model={"name": model_name, "templates": updates},
            )
        return {"name": model_name, "template": normalized, "updated": True}

    def set_notetype_css(self, name: str, css: str) -> dict[str, JSONValue]:
        model_name = name.strip()
        try:
            self._invoke("updateModelStyling", model=model_name, css=css)
        except AnkiConnectAPIError:
            self._invoke(
                "updateModelStyling",
                model={"name": model_name, "css": css},
            )
        return {"name": model_name, "updated": True, "css": css}

    # Notes
    def add_note(
        self,
        deck: str,
        notetype: str,
        fields: dict[str, str],
        tags: list[str] | None = None,
        allow_duplicate: bool = False,
    ) -> int:
        payload = {
            "deckName": deck,
            "modelName": notetype,
            "fields": fields,
            "tags": self._normalize_tags(tags),
        }
        if allow_duplicate:
            payload["options"] = {"allowDuplicate": True}
        result = self._invoke("addNote", note=payload)
        return self._as_int(result, "addNote result")

    def add_notes(self, notes: list[dict[str, JSONValue]]) -> list[int | None]:
        anki_notes: list[dict[str, JSONValue]] = []

        for item in notes:
            deck = str(item.get("deck") or item.get("deckName") or "")
            notetype = str(item.get("notetype") or item.get("modelName") or "")
            fields_raw = item.get("fields")
            tags_raw = item.get("tags")

            if not deck or not notetype:
                raise AnkiConnectProtocolError(
                    "Each bulk note must include deck/deckName and notetype/modelName."
                )
            if not isinstance(fields_raw, dict):
                raise AnkiConnectProtocolError("Each bulk note must include a fields object.")

            fields: dict[str, str] = {str(k): str(v) for k, v in fields_raw.items()}
            tags = self._normalize_tags(self._coerce_tag_input(tags_raw))

            anki_notes.append(
                {
                    "deckName": deck,
                    "modelName": notetype,
                    "fields": fields,
                    "tags": tags,
                }
            )

        result = self._invoke("addNotes", notes=anki_notes)
        if not isinstance(result, list):
            raise AnkiConnectProtocolError("addNotes must return a list.")

        output: list[int | None] = []
        for value in result:
            if value is None:
                output.append(None)
            else:
                output.append(self._as_int(value, "addNotes item"))

        return output

    def update_note(
        self,
        note_id: int,
        fields: dict[str, str] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, JSONValue]:
        updated_fields = False
        updated_tags = False

        if fields:
            self._invoke("updateNoteFields", note={"id": note_id, "fields": fields})
            updated_fields = True

        if tags is not None:
            desired_tags = set(self._normalize_tags(tags))
            current_note = self.get_note(note_id)
            current_tags = set(self._extract_tags(current_note.get("tags")))

            to_add = sorted(desired_tags - current_tags)
            to_remove = sorted(current_tags - desired_tags)

            if to_add:
                self._invoke("addTags", notes=[note_id], tags=" ".join(to_add))
            if to_remove:
                self._invoke("removeTags", notes=[note_id], tags=" ".join(to_remove))

            updated_tags = True

        return {
            "note_id": note_id,
            "updated_fields": updated_fields,
            "updated_tags": updated_tags,
        }

    def delete_notes(self, note_ids: list[int]) -> dict[str, JSONValue]:
        ids = self._normalize_ids(note_ids)
        if not ids:
            return {"deleted": 0}

        self._invoke("deleteNotes", notes=ids)
        return {"deleted": len(ids), "note_ids": ids}

    def find_notes(self, query: str) -> list[int]:
        result = self._invoke("findNotes", query=query)
        return self._as_int_list(result, "findNotes")

    def get_note(self, note_id: int) -> dict[str, JSONValue]:
        result = self._invoke("notesInfo", notes=[note_id])
        if not isinstance(result, list) or not result:
            raise AnkiConnectProtocolError("notesInfo returned no rows.")
        first = result[0]
        return self._as_json_object(first, "notesInfo row")

    def get_note_fields(self, note_id: int, fields: list[str] | None = None) -> dict[str, str]:
        note = self.get_note(note_id)
        raw_fields = note.get("fields")
        if not isinstance(raw_fields, Mapping):
            raise AnkiConnectProtocolError("notesInfo.fields must be an object.")

        values: dict[str, str] = {}
        for k, v in raw_fields.items():
            if isinstance(v, Mapping):
                v_map = cast(Mapping[str, JSONValue], v)
                values[str(k)] = str(v_map.get("value") or "")
            else:
                values[str(k)] = str(v)

        if fields:
            wanted = {f.strip() for f in fields if f.strip()}
            return {k: v for k, v in values.items() if k in wanted}
        return values

    # Cards
    def find_cards(self, query: str) -> list[int]:
        result = self._invoke("findCards", query=query)
        return self._as_int_list(result, "findCards")

    def get_card(self, card_id: int) -> dict[str, JSONValue]:
        result = self._invoke("cardsInfo", cards=[card_id])
        if not isinstance(result, list) or not result:
            raise AnkiConnectProtocolError("cardsInfo returned no rows.")
        first = result[0]
        return self._as_json_object(first, "cardsInfo row")

    def answer_card(self, card_id: int, ease: int) -> dict[str, JSONValue]:
        if ease not in {1, 2, 3, 4}:
            raise AnkiConnectProtocolError("ease must be 1, 2, 3, or 4.")

        current = self._invoke("guiCurrentCard")
        current_obj = self._as_json_object(current, "guiCurrentCard")
        if "cardId" not in current_obj:
            raise AnkiConnectAPIError("guiCurrentCard", "No current GUI card is active.")

        current_id = self._as_int(current_obj["cardId"], "guiCurrentCard.cardId")
        if current_id != card_id:
            raise AnkiConnectAPIError(
                "guiAnswerCard",
                f"Requested card {card_id} is not active in GUI (current: {current_id}).",
            )

        try:
            self._invoke("guiAnswerCard", ease=ease)
        except AnkiConnectAPIError:
            # Some AnkiConnect versions use answerEase.
            self._invoke("guiAnswerCard", answerEase=ease)

        return {"card_id": card_id, "ease": ease, "answered": True}

    def suspend_cards(self, card_ids: list[int]) -> dict[str, JSONValue]:
        ids = self._normalize_ids(card_ids)
        if not ids:
            return {"suspended": 0}
        self._invoke("suspend", cards=ids)
        return {"suspended": len(ids), "card_ids": ids}

    def unsuspend_cards(self, card_ids: list[int]) -> dict[str, JSONValue]:
        ids = self._normalize_ids(card_ids)
        if not ids:
            return {"unsuspended": 0}
        self._invoke("unsuspend", cards=ids)
        return {"unsuspended": len(ids), "card_ids": ids}

    def get_revlog(self, card_id: int, limit: int = 50) -> list[dict[str, JSONValue]]:
        raise NotImplementedError("Revlog read is not supported via AnkiConnect backend.")

    def move_cards(self, card_ids: list[int], deck: str) -> dict[str, JSONValue]:
        ids = self._normalize_ids(card_ids)
        if not ids:
            return {"moved": 0, "card_ids": []}
        self._invoke("changeDeck", cards=ids, deck=deck)
        return {"moved": len(ids), "card_ids": ids, "deck": deck}

    def set_card_flag(self, card_ids: list[int], flag: int) -> dict[str, JSONValue]:
        if flag < 0 or flag > 7:
            raise ValueError("flag must be in range 0..7")
        ids = self._normalize_ids(card_ids)
        if not ids:
            return {"updated": 0, "card_ids": []}
        # AnkiConnect does not reliably expose a "setFlag" action across versions.
        # The documented/supported way is to set the card "flags" field.
        failures: list[dict[str, JSONValue]] = []
        for cid in ids:
            result = self._invoke(
                "setSpecificValueOfCard",
                card=cid,
                keys=["flags"],
                newValues=[flag],
                warning_check=True,
            )
            if not isinstance(result, list) or not result:
                raise AnkiConnectProtocolError(
                    "setSpecificValueOfCard must return a non-empty list."
                )

            first = result[0]
            if first is True:
                continue

            failures.append({"card_id": int(cid), "result": cast(JSONValue, first)})

        if failures:
            raise AnkiConnectAPIError(
                "setSpecificValueOfCard",
                f"Failed to set flags for {len(failures)} card(s).",
            )

        return {"updated": len(ids), "card_ids": ids, "flag": flag}

    def bury_cards(self, card_ids: list[int]) -> dict[str, JSONValue]:
        ids = self._normalize_ids(card_ids)
        if not ids:
            return {"buried": 0, "card_ids": []}
        self._invoke("bury", cards=ids)
        return {"buried": len(ids), "card_ids": ids}

    def unbury_cards(self, deck: str | None = None) -> dict[str, JSONValue]:
        if deck is None:
            try:
                self._invoke("unbury")
                return {"unburied": True, "scope": "all"}
            except AnkiConnectAPIError:
                self._invoke("unburyCards", cards=[])
                return {"unburied": True, "scope": "all"}

        ids = self.find_cards(f'deck:"{deck}" is:buried')
        if not ids:
            return {"unburied": 0, "deck": deck, "card_ids": []}
        try:
            self._invoke("unburyCards", cards=ids)
        except AnkiConnectAPIError:
            self._invoke("unbury")
        return {"unburied": len(ids), "deck": deck, "card_ids": ids}

    def reschedule_cards(self, card_ids: list[int], days: int) -> dict[str, JSONValue]:
        if days < 0:
            raise ValueError("days must be >= 0")
        ids = self._normalize_ids(card_ids)
        if not ids:
            return {"rescheduled": 0, "card_ids": []}
        self._invoke("setDueDate", cards=ids, days=str(days))
        return {"rescheduled": len(ids), "card_ids": ids, "days": days}

    def reset_cards(self, card_ids: list[int]) -> dict[str, JSONValue]:
        ids = self._normalize_ids(card_ids)
        if not ids:
            return {"reset": 0, "card_ids": []}
        self._invoke("forgetCards", cards=ids)
        return {"reset": len(ids), "card_ids": ids}

    # Tags
    def get_tags(self) -> list[str]:
        result = self._invoke("getTags")
        return self._as_str_list(result, "getTags")

    def add_tags(self, note_ids: list[int], tags: list[str]) -> dict[str, JSONValue]:
        ids = self._normalize_ids(note_ids)
        normalized_tags = self._normalize_tags(tags)

        if not ids or not normalized_tags:
            return {"updated": 0}

        self._invoke("addTags", notes=ids, tags=" ".join(normalized_tags))
        return {"updated": len(ids), "note_ids": ids, "tags": normalized_tags}

    def remove_tags(self, note_ids: list[int], tags: list[str]) -> dict[str, JSONValue]:
        ids = self._normalize_ids(note_ids)
        normalized_tags = self._normalize_tags(tags)

        if not ids or not normalized_tags:
            return {"updated": 0}

        self._invoke("removeTags", notes=ids, tags=" ".join(normalized_tags))
        return {"updated": len(ids), "note_ids": ids, "tags": normalized_tags}

    def get_tag_counts(self) -> list[dict[str, JSONValue]]:
        tags = self.get_tags()
        items: list[dict[str, JSONValue]] = []
        for tag in sorted(tags, key=str.lower):
            note_ids = self.find_notes(f'tag:"{tag}"')
            items.append({"tag": tag, "count": len(note_ids)})
        return items

    def rename_tag(self, old_tag: str, new_tag: str) -> dict[str, JSONValue]:
        source = old_tag.strip()
        target = new_tag.strip()
        if not source or not target:
            raise ValueError("Both tags are required.")
        ids = self.find_notes(f'tag:"{source}"')
        if not ids:
            return {"from": source, "to": target, "updated": 0}
        self._invoke("addTags", notes=ids, tags=target)
        self._invoke("removeTags", notes=ids, tags=source)
        return {"from": source, "to": target, "updated": len(ids), "note_ids": ids}

    # Review summary
    def get_due_counts(self, deck: str | None = None) -> dict[str, int]:
        prefix = self._deck_query_prefix(deck)
        new_count = len(self.find_cards(f"{prefix}is:due is:new"))
        learn_count = len(self.find_cards(f"{prefix}is:due is:learn"))
        review_count = len(self.find_cards(f"{prefix}is:due is:review"))

        return {
            "new": new_count,
            "learn": learn_count,
            "review": review_count,
            "total": new_count + learn_count + review_count,
        }

    # Helpers
    def _validate_url(self, *, url: str, allow_non_localhost: bool) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise AnkiConnectProtocolError("AnkiConnect URL must use http:// or https://.")
        if not parsed.netloc:
            raise AnkiConnectProtocolError("AnkiConnect URL must include host and port.")

        host = parsed.hostname
        if host is None:
            raise AnkiConnectProtocolError("AnkiConnect URL host is invalid.")
        if not allow_non_localhost and host not in _LOCAL_HOSTS:
            raise AnkiConnectProtocolError(
                "Refusing non-localhost AnkiConnect URL for safety. "
                "Set allow_non_localhost=True to override."
            )

    def _as_int(self, value: JSONValue, field: str) -> int:
        if not isinstance(value, int):
            raise AnkiConnectProtocolError(f"{field} must be int, got {type(value).__name__}.")
        return value

    def _as_json_object(self, value: object, field: str) -> dict[str, JSONValue]:
        if not isinstance(value, Mapping):
            raise AnkiConnectProtocolError(f"{field} must be an object.")
        return {str(k): cast(JSONValue, v) for k, v in value.items()}

    def _as_int_list(self, value: JSONValue, field: str) -> list[int]:
        if not isinstance(value, list):
            raise AnkiConnectProtocolError(f"{field} must be a list.")
        output: list[int] = []
        for item in value:
            if not isinstance(item, int):
                raise AnkiConnectProtocolError(f"{field} items must be int.")
            output.append(item)
        return output

    def _as_str_list(self, value: JSONValue, field: str) -> list[str]:
        if not isinstance(value, list):
            raise AnkiConnectProtocolError(f"{field} must be a list.")
        output: list[str] = []
        for item in value:
            output.append(str(item))
        return output

    def _normalize_ids(self, values: list[int]) -> list[int]:
        seen: set[int] = set()
        output: list[int] = []
        for value in values:
            if not isinstance(value, int):
                raise AnkiConnectProtocolError("IDs must be integers.")
            if value in seen:
                continue
            seen.add(value)
            output.append(value)
        return output

    def _normalize_tags(self, tags: list[str] | None) -> list[str]:
        if not tags:
            return []

        seen: set[str] = set()
        output: list[str] = []
        for tag in tags:
            normalized = tag.strip()
            if not normalized:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            output.append(normalized)
        return output

    def _coerce_tag_input(self, raw: JSONValue) -> list[str]:
        if raw is None:
            return []
        if isinstance(raw, list):
            return [str(item) for item in raw]
        if isinstance(raw, str):
            return [item for item in raw.replace(",", " ").split(" ") if item]
        raise AnkiConnectProtocolError("tags must be list[str] or string.")

    def _extract_tags(self, raw: JSONValue) -> list[str]:
        if raw is None:
            return []
        if isinstance(raw, list):
            return [str(item) for item in raw]
        if isinstance(raw, str):
            if not raw.strip():
                return []
            return [item for item in raw.replace(",", " ").split(" ") if item]
        return []

    def _deck_query_prefix(self, deck: str | None) -> str:
        if deck is None:
            return ""
        escaped = deck.replace("\\", "\\\\").replace('"', '\\"')
        return f'deck:"{escaped}" '