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
            response = self._client.post(self._url, json=payload)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise AnkiConnectUnavailableError(
                f"Timeout contacting AnkiConnect at {self._url}."
            ) from exc
        except httpx.ConnectError as exc:
            raise AnkiConnectUnavailableError(
                f"Cannot connect to AnkiConnect at {self._url}. Is Anki running with the add-on?"
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

    def create_deck(self, name: str) -> dict[str, JSONValue]:
        self._invoke("createDeck", deck=name)
        return {"deck": name, "created": True}

    def delete_deck(self, name: str) -> dict[str, JSONValue]:
        self._invoke("deleteDecks", decks=[name], cardsToo=True)
        return {"deck": name, "deleted": True, "cards_deleted": True}

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

        try:
            styling = self._invoke("modelStyling", modelName=name)
            if isinstance(styling, dict):
                result["styling"] = styling
        except AnkiConnectAPIError:
            # Not all AnkiConnect versions expose modelStyling.
            result["styling"] = {}

        return result

    # Notes
    def add_note(
        self,
        deck: str,
        notetype: str,
        fields: dict[str, str],
        tags: list[str] | None = None,
    ) -> int:
        payload = {
            "deckName": deck,
            "modelName": notetype,
            "fields": fields,
            "tags": self._normalize_tags(tags),
        }
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