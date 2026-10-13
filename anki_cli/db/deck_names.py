"""Deck-name spellings (#75). Anki >= 2.1.28 stores a hierarchical deck name in
``decks.name`` with ``\\x1f`` between components (rslib ``NativeDeckName``);
``::`` is the form people type and see. Pure string helpers: this module
imports nothing from the package, so the SQL compiler can use it cheaply.
"""

from __future__ import annotations

DECK_SEP_NATIVE = "\x1f"
DECK_SEP_HUMAN = "::"


def deck_human_to_native(name: str) -> str:
    """``A::B`` -> ``A\\x1fB``.

    ``replace`` scans left to right, so a component that ends in ``:`` is
    ambiguous in the human form (``Ch 1:`` + ``Verbs`` reads ``Ch 1:::Verbs``);
    lookups that hold a connection resolve that case against the stored names
    (``DeckLookupMixin._native_deck_name``).
    """
    return name.replace(DECK_SEP_HUMAN, DECK_SEP_NATIVE)


def deck_native_to_human(name: str) -> str:
    return name.replace(DECK_SEP_NATIVE, DECK_SEP_HUMAN)
