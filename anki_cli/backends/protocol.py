from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from anki_cli.models.output import JSONValue


class BackendUnsupportedError(NotImplementedError):
    """The backend cannot perform this operation at all (not a transient failure).

    Raised by ``AnkiConnectBackend`` for the scheduler-introspection methods
    only the direct SQLite backend can implement. Callers that can degrade
    should check ``supports_scheduler_introspection`` first instead of catching.
    """

    def __init__(self, operation: str, backend: str, hint: str | None = None) -> None:
        self.operation = operation
        self.backend = backend
        message = f"{operation} is not supported by the {backend} backend."
        if hint:
            message = f"{message} {hint}"
        super().__init__(message)


@runtime_checkable
class AnkiBackend(Protocol):
    """Backend contract shared by ankiconnect and direct modes.

    Every method exists on both backends. The four under "Scheduler
    introspection" are only *implementable* on the direct backend (they read and
    write scheduler state that AnkiConnect does not expose); the AnkiConnect
    backend raises ``BackendUnsupportedError`` and reports
    ``supports_scheduler_introspection = False`` so callers can branch on a
    capability rather than on ``backend.name``.
    """

    name: str
    collection_path: Path | None
    supports_scheduler_introspection: bool

    # Decks
    def get_decks(self) -> list[dict[str, JSONValue]]: ...
    def get_deck(self, name: str) -> dict[str, JSONValue]: ...
    def create_deck(self, name: str) -> dict[str, JSONValue]: ...
    def rename_deck(self, old_name: str, new_name: str) -> dict[str, JSONValue]: ...
    def delete_deck(self, name: str) -> dict[str, JSONValue]: ...
    def get_deck_config(self, name: str) -> dict[str, JSONValue]: ...
    def set_deck_config(
        self,
        name: str,
        updates: dict[str, JSONValue],
    ) -> dict[str, JSONValue]: ...

    # Notetypes
    def get_notetypes(self) -> list[dict[str, JSONValue]]: ...
    def get_notetype(self, name: str) -> dict[str, JSONValue]: ...
    def create_notetype(
        self,
        name: str,
        fields: list[str],
        templates: list[dict[str, str]],
        *,
        css: str = "",
        kind: str = "normal",
    ) -> dict[str, JSONValue]: ...
    def add_notetype_field(self, name: str, field_name: str) -> dict[str, JSONValue]: ...
    def remove_notetype_field(self, name: str, field_name: str) -> dict[str, JSONValue]: ...
    def add_notetype_template(
        self,
        name: str,
        template_name: str,
        front: str,
        back: str,
    ) -> dict[str, JSONValue]: ...
    def edit_notetype_template(
        self,
        name: str,
        template_name: str,
        *,
        front: str | None = None,
        back: str | None = None,
    ) -> dict[str, JSONValue]: ...
    def set_notetype_css(self, name: str, css: str) -> dict[str, JSONValue]: ...

    # Notes
    def add_note(
        self,
        deck: str,
        notetype: str,
        fields: dict[str, str],
        tags: list[str] | None = None,
        allow_duplicate: bool = False,
    ) -> int: ...
    def add_notes(
        self,
        notes: list[dict[str, JSONValue]],
        *,
        allow_duplicate: bool = False,
    ) -> list[int | None]: ...
    def update_note(
        self,
        note_id: int,
        fields: dict[str, str] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, JSONValue]: ...
    def delete_notes(self, note_ids: list[int]) -> dict[str, JSONValue]: ...
    def find_notes(self, query: str) -> list[int]: ...
    def get_note(self, note_id: int) -> dict[str, JSONValue]: ...
    def get_note_fields(
        self,
        note_id: int,
        fields: list[str] | None = None,
    ) -> dict[str, str]: ...

    # Cards
    def find_cards(self, query: str) -> list[int]: ...
    def get_card(self, card_id: int) -> dict[str, JSONValue]: ...
    def answer_card(self, card_id: int, ease: int) -> dict[str, JSONValue]:
        """Answer ``card_id`` with ``ease`` 1..4.

        The two backends mean different things by this. **Direct** runs the
        local FSRS scheduler against the collection: any card, any time.
        **AnkiConnect** can only answer the card Anki Desktop is *currently
        showing* (``guiCurrentCard``); asking for any other card raises
        ``AnkiConnectAPIError``. Callers that need "answer arbitrary card"
        semantics need the direct backend.
        """
        ...
    def suspend_cards(self, card_ids: list[int]) -> dict[str, JSONValue]: ...
    def unsuspend_cards(self, card_ids: list[int]) -> dict[str, JSONValue]: ...
    def get_revlog(self, card_id: int, limit: int = 50) -> list[dict[str, JSONValue]]: ...
    def move_cards(self, card_ids: list[int], deck: str) -> dict[str, JSONValue]: ...
    def set_card_flag(self, card_ids: list[int], flag: int) -> dict[str, JSONValue]: ...
    def bury_cards(self, card_ids: list[int]) -> dict[str, JSONValue]: ...
    def unbury_cards(self, deck: str | None = None) -> dict[str, JSONValue]: ...
    def reschedule_cards(self, card_ids: list[int], days: int) -> dict[str, JSONValue]: ...
    def reset_cards(self, card_ids: list[int]) -> dict[str, JSONValue]: ...

    # Tags
    def get_tags(self) -> list[str]: ...
    def add_tags(self, note_ids: list[int], tags: list[str]) -> dict[str, JSONValue]: ...
    def remove_tags(self, note_ids: list[int], tags: list[str]) -> dict[str, JSONValue]: ...
    def get_tag_counts(self) -> list[dict[str, JSONValue]]: ...
    def rename_tag(self, old_tag: str, new_tag: str) -> dict[str, JSONValue]: ...

    # Review summary
    def get_due_counts(self, deck: str | None = None) -> dict[str, int]: ...

    # Scheduler introspection (direct backend only; see class docstring)
    def get_next_due_card(self, deck: str | None = None) -> dict[str, JSONValue]:
        """``{"card_id": int | None, "kind": "learn_due" | "review_due" | "new" | "none"}``
        for the card Anki would show next, using the scheduler's own ordering."""
        ...

    def preview_ratings(self, card_id: int) -> list[dict[str, JSONValue]]:
        """What each of the four ratings would do to ``card_id`` (interval,
        due, state) without answering it."""
        ...

    def snapshot_card_state(self, card_id: int) -> dict[str, JSONValue]:
        """Everything ``restore_card_state`` needs to undo an answer to this card."""
        ...

    def restore_card_state(self, snapshot: Mapping[str, Any]) -> dict[str, JSONValue]:
        """Put a card back as it was in ``snapshot`` and delete the revlog row the
        answer wrote."""
        ...
