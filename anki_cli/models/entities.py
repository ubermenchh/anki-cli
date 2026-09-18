"""Canonical shapes for the objects backends return (#30).

The direct SQLite backend's key set is the canonical one. The AnkiConnect
backend normalises its raw ``cardsInfo`` / ``notesInfo`` / model responses to
these keys (``backends/normalize.py``) while keeping its own extra keys, so a
consumer — an agent parsing ``--format json``, or this CLI's own renderers —
reads one shape regardless of backend.

These models are the *definition* of the contract. Backends still return plain
``dict[str, JSONValue]`` (the models are not on the Protocol signatures); the
models validate those dicts in tests and document the keys in SKILL.md. Every
model allows extra keys: backends may add more, never less.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from anki_cli.models.output import JSONValue

DueKind = Literal[
    "new_position",
    "learn_epoch_secs",
    "learn_day_index",
    "review_day_index",
    "raw",
]


class DueInfo(BaseModel):
    """``cards.due`` decoded into a unit you can compare.

    Anki stores three different things in the one ``due`` column depending on
    the card's type/queue: a position (new), an epoch second (intraday learn),
    or a day index relative to the collection's creation (review, day-learn).
    ``kind`` says which; ``raw`` is the stored value; the typed key
    (``position`` / ``epoch_secs`` / ``day_index``) is the decoded one.
    ``epoch_secs`` and ``days_from_today`` are present for day-index kinds
    only when the backend knows the collection's rollover timing (direct
    always; AnkiConnect never, since the API does not expose it).
    """

    model_config = ConfigDict(extra="allow")

    kind: DueKind
    raw: int
    position: int | None = None
    epoch_secs: int | None = None
    day_index: int | None = None
    days_from_today: int | None = None


class Card(BaseModel):
    """A card as returned by ``get_card`` and inside ``cards``' ``items``."""

    model_config = ConfigDict(extra="allow")

    cardId: int
    note: int = Field(description="Note id")
    deckName: str
    notetype_name: str
    ord: int
    type: int
    queue: int
    due: int
    interval: int
    factor: int
    reps: int
    lapses: int
    left: int
    fields: list[str] = Field(description="Field values in notetype order")
    tags: list[str]
    due_info: DueInfo
    # Direct-only today; AnkiConnect cannot read them.
    deckId: int | None = None
    notetype_id: int | None = None
    flags: int | None = None


class Note(BaseModel):
    """A note as returned by ``get_note``."""

    model_config = ConfigDict(extra="allow")

    id: int
    mid: int | None = Field(default=None, description="Notetype id (direct only)")
    notetype_name: str | None = None
    mod: int
    tags: list[str]
    fields: list[str] = Field(description="Field values in notetype order")
    field_names: list[str] | None = Field(
        default=None, description="Names matching ``fields``; AnkiConnect supplies them"
    )


class Deck(BaseModel):
    """A deck as returned by ``get_decks`` (list) and ``get_deck`` (with counts)."""

    model_config = ConfigDict(extra="allow")

    id: int
    name: str
    kind: Literal["normal", "filtered", "unknown"] = "unknown"
    due_counts: dict[str, int] | None = None


class Notetype(BaseModel):
    """A notetype as returned by ``get_notetype``."""

    model_config = ConfigDict(extra="allow")

    id: int | None = Field(default=None, description="AnkiConnect does not expose it")
    name: str
    kind: Literal["normal", "cloze"]
    fields: list[str]
    templates: dict[str, dict[str, JSONValue]] = Field(
        description="template name -> {Front, Back, ord}"
    )
    styling: dict[str, JSONValue]


__all__ = ["Card", "Deck", "DueInfo", "DueKind", "Note", "Notetype"]
