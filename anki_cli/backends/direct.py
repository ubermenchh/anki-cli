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

    def get_deck(self, name: str) -> dict[str, JSONValue]:
        return self._store.get_deck(name)

    def get_notetypes(self) -> list[dict[str, JSONValue]]:
        return self._store.get_notetypes()

    def get_notetype(self, name: str) -> dict[str, JSONValue]:
        return self._store.get_notetype(name)

    def create_notetype(
        self,
        name: str,
        fields: list[str],
        templates: list[dict[str, str]],
        *,
        css: str = "",
        kind: str = "normal",
    ) -> dict[str, JSONValue]:
        return self._store.create_notetype(
            name=name,
            fields=fields,
            templates=templates,
            css=css,
            kind=kind,
        )

    def add_notetype_field(self, name: str, field_name: str) -> dict[str, JSONValue]:
        return self._store.add_notetype_field(name=name, field_name=field_name)

    def remove_notetype_field(self, name: str, field_name: str) -> dict[str, JSONValue]:
        return self._store.remove_notetype_field(name=name, field_name=field_name)

    def add_notetype_template(
        self,
        name: str,
        template_name: str,
        front: str,
        back: str,
    ) -> dict[str, JSONValue]:
        return self._store.add_notetype_template(
            name=name,
            template_name=template_name,
            front=front,
            back=back,
        )

    def edit_notetype_template(
        self,
        name: str,
        template_name: str,
        *,
        front: str | None = None,
        back: str | None = None,
    ) -> dict[str, JSONValue]:
        return self._store.edit_notetype_template(
            name=name,
            template_name=template_name,
            front=front,
            back=back,
        )

    def set_notetype_css(self, name: str, css: str) -> dict[str, JSONValue]:
        return self._store.set_notetype_css(name=name, css=css)

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

    def rename_deck(self, old_name: str, new_name: str) -> dict[str, JSONValue]:
        return self._store.rename_deck(old_name=old_name, new_name=new_name)

    def delete_deck(self, name: str) -> dict[str, JSONValue]:
        return self._store.delete_deck(name)

    def get_deck_config(self, name: str) -> dict[str, JSONValue]:
        return self._store.get_deck_config(name)

    def set_deck_config(
        self,
        name: str,
        updates: dict[str, JSONValue],
    ) -> dict[str, JSONValue]:
        return self._store.set_deck_config(name=name, updates=updates)

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