"""Typed refusals raised by the direct store.

Callers (``cli/errors.py``, ``backends/factory.py``) catch these by type
instead of matching message text.
"""

from __future__ import annotations


class DirectWriteBlockedError(RuntimeError):
    """Direct write refused: Anki Desktop is running or holds the collection lock.

    Raised by ``_ensure_write_safe`` so callers can catch the refusal by type
    instead of matching the message text.
    """


# Lowest collection schema this module understands: separate decks / notetypes /
# fields / templates / deck_config tables holding protobuf blobs. Anki upgrades a
# profile to it on first open with 2.1.50+ (2022); older files (schema 11) keep
# everything as JSON inside the col table and have none of those tables.
MIN_SUPPORTED_SCHEMA_VERSION = 18


class UnsupportedCollectionError(RuntimeError):
    """The collection file is real but its schema is older than we can read or write."""


class NoteRejectedError(ValueError):
    """``add_note`` refused the note before writing anything.

    Base for the per-note refusals that mirror AnkiConnect's ``addNote`` checks
    (rslib ``note_fields_check``); ``add_notes`` treats these as per-item failures
    and reports ``None`` for the item, while every other error propagates.
    """


class DuplicateNoteError(NoteRejectedError):
    """A note of the same notetype already has this first field.

    Mirrors AnkiConnect's "cannot create note because it is a duplicate"; lifted by
    ``allow_duplicate``. The message states the fact only — the CLI layer appends
    the ``--allow-duplicate`` remedy, since this module has no CLI surface.
    """

    # ``duplicate_ids`` always carries the full list; the message stays one readable
    # line even after a large ``--allow-duplicate`` import.
    MAX_IDS_IN_MESSAGE = 10

    def __init__(self, *, notetype: str, duplicate_ids: list[int]) -> None:
        self.notetype = notetype
        self.duplicate_ids = duplicate_ids
        shown = duplicate_ids[: self.MAX_IDS_IN_MESSAGE]
        ids = ", ".join(str(i) for i in shown)
        if len(duplicate_ids) > len(shown):
            ids += f" and {len(duplicate_ids) - len(shown)} more"
        super().__init__(
            f"Duplicate note: first field matches existing note(s) {ids} "
            f"in notetype '{notetype}'."
        )


class EmptyNoteError(NoteRejectedError):
    """The first field is empty once markup is ignored.

    Mirrors AnkiConnect's "cannot create note because it is empty" (rslib
    ``NoteFieldsState::Empty``). Not lifted by ``allow_duplicate``.
    """

    def __init__(self, *, notetype: str, field_name: str) -> None:
        self.notetype = notetype
        self.field_name = field_name
        super().__init__(
            f"Empty note: first field '{field_name}' of notetype '{notetype}' is empty."
        )
