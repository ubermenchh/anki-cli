from __future__ import annotations

from pathlib import Path

from anki_cli.backends.protocol import AnkiBackend, JSONValue
from anki_cli.db.anki_direct import AnkiDirectReadStore


class DirectBackend(AnkiBackend):
    """
    Direct DB backend bootstrap.

    Read/write helpers that operate directly on Anki's SQLite collection.
    """

    name = "direct"

    def __init__(self, collection_path: Path) -> None:
        resolved = collection_path.expanduser().resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"Direct collection not found: {resolved}")
        self.collection_path = resolved
        self._store = AnkiDirectReadStore(resolved)

    # ---- decks / notetypes ----

    def get_decks(self) -> list[dict[str, JSONValue]]:
        return self._store.get_decks()

    def get_notetypes(self) -> list[dict[str, JSONValue]]:
        return self._store.get_notetypes()

    def get_notetype(self, name: str) -> dict[str, JSONValue]:
        return self._store.get_notetype(name)

    # ---- notes ----

    def find_notes(self, query: str) -> list[int]:
        return self._store.find_note_ids(query)

    def get_note(self, note_id: int) -> dict[str, JSONValue]:
        return self._store.get_note(note_id)

    # ---- cards ----

    def find_cards(self, query: str) -> list[int]:
        return self._store.find_card_ids(query)

    def get_card(self, card_id: int) -> dict[str, JSONValue]:
        return self._store.get_card(card_id)

    def get_revlog(self, card_id: int, limit: int = 50) -> list[dict[str, JSONValue]]:
        return self._store.get_revlog(card_id=card_id, limit=limit)

    # ---- tags / due ----

    def get_tags(self) -> list[str]:
        return self._store.get_tags()

    def get_due_counts(self, deck: str | None = None) -> dict[str, int]:
        return self._store.get_due_counts(deck)

    # ---- write/scheduling operations ----

    def create_deck(self, name: str) -> dict[str, JSONValue]:
        return self._store.create_deck(name)

    def delete_deck(self, name: str) -> dict[str, JSONValue]:
        return self._store.delete_deck(name)

    def add_note(
        self,
        deck: str,
        notetype: str,
        fields: dict[str, str],
        tags: list[str] | None = None,
    ) -> int:
        return self._store.add_note(
            deck=deck,
            notetype=notetype,
            fields=fields,
            tags=tags,
        )

    def add_notes(self, notes: list[dict[str, JSONValue]]) -> list[int | None]:
        return self._store.add_notes(notes)

    def update_note(
        self,
        note_id: int,
        fields: dict[str, str] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, JSONValue]:
        return self._store.update_note(note_id=note_id, fields=fields, tags=tags)

    def delete_notes(self, note_ids: list[int]) -> dict[str, JSONValue]:
        return self._store.delete_notes(note_ids)

    def answer_card(self, card_id: int, ease: int) -> dict[str, JSONValue]:
        return self._store.answer_card(card_id=card_id, ease=ease)

    def suspend_cards(self, card_ids: list[int]) -> dict[str, JSONValue]:
        return self._store.suspend_cards(card_ids)

    def unsuspend_cards(self, card_ids: list[int]) -> dict[str, JSONValue]:
        return self._store.unsuspend_cards(card_ids)

    def add_tags(self, note_ids: list[int], tags: list[str]) -> dict[str, JSONValue]:
        return self._store.add_tags(note_ids, tags)

    def remove_tags(self, note_ids: list[int], tags: list[str]) -> dict[str, JSONValue]:
        return self._store.remove_tags(note_ids, tags)