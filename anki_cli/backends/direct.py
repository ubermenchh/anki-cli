from __future__ import annotations

from pathlib import Path

from anki_cli.backends.protocol import AnkiBackend, JSONValue
from anki_cli.db.anki_direct import AnkiDirectReadStore


class DirectBackend(AnkiBackend):
    """
    Direct DB backend bootstrap.

    Read paths are implemented.
    Write/scheduling paths are intentionally blocked until protobuf + write safety are done.
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

    # ---- tags / due ----

    def get_tags(self) -> list[str]:
        return self._store.get_tags()

    def get_due_counts(self, deck: str | None = None) -> dict[str, int]:
        return self._store.get_due_counts(deck)

    # ---- write operations (phase 3 write path not done yet) ----

    def create_deck(self, name: str) -> dict[str, JSONValue]:
        raise NotImplementedError("Direct backend write path not implemented yet.")

    def delete_deck(self, name: str) -> dict[str, JSONValue]:
        raise NotImplementedError("Direct backend write path not implemented yet.")

    def add_note(
        self,
        deck: str,
        notetype: str,
        fields: dict[str, str],
        tags: list[str] | None = None,
    ) -> int:
        raise NotImplementedError("Direct backend write path not implemented yet.")

    def add_notes(self, notes: list[dict[str, JSONValue]]) -> list[int | None]:
        raise NotImplementedError("Direct backend write path not implemented yet.")

    def update_note(
        self,
        note_id: int,
        fields: dict[str, str] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, JSONValue]:
        raise NotImplementedError("Direct backend write path not implemented yet.")

    def delete_notes(self, note_ids: list[int]) -> dict[str, JSONValue]:
        raise NotImplementedError("Direct backend write path not implemented yet.")

    def answer_card(self, card_id: int, ease: int) -> dict[str, JSONValue]:
        raise NotImplementedError("Direct backend scheduler path not implemented yet.")

    def suspend_cards(self, card_ids: list[int]) -> dict[str, JSONValue]:
        raise NotImplementedError("Direct backend write path not implemented yet.")

    def unsuspend_cards(self, card_ids: list[int]) -> dict[str, JSONValue]:
        raise NotImplementedError("Direct backend write path not implemented yet.")

    def add_tags(self, note_ids: list[int], tags: list[str]) -> dict[str, JSONValue]:
        raise NotImplementedError("Direct backend write path not implemented yet.")

    def remove_tags(self, note_ids: list[int], tags: list[str]) -> dict[str, JSONValue]:
        raise NotImplementedError("Direct backend write path not implemented yet.")