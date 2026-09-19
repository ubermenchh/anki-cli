"""Deck lookups shared by the card, note and scheduling paths: name -> id,
the subtree ``did IN (...)`` filter, and a deck's options preset.
"""

from __future__ import annotations

import sqlite3
from typing import NamedTuple

import betterproto

from anki_cli.db.connection import ConnectionMixin
from anki_cli.models.output import JSONValue
from anki_cli.proto.anki.deck_config import DeckConfigConfig


class DeckSubtree(NamedTuple):
    """A deck and its descendants, as ``_deck_subtree`` found them."""

    name: str
    """The parent's name as stored (the caller may have passed another case)."""
    rows: list[sqlite3.Row]
    """``id, name, kind`` per deck, parents first (by name length, then name)."""


class DeckLookupMixin(ConnectionMixin):
    """Deck lookups shared by the card and note paths."""

    def _resolve_deck_id(self, conn: sqlite3.Connection, deck_name: str) -> int:
        row = conn.execute(
            "SELECT id FROM decks WHERE name = ?",
            (deck_name.strip(),),
        ).fetchone()
        if row is None:
            raise LookupError(f"Deck not found: {deck_name}")
        return int(row["id"])

    def _deck_subtree(self, conn: sqlite3.Connection, name: str) -> DeckSubtree | None:
        """The deck called ``name`` plus every descendant, or ``None`` if there is
        no such deck. This is the one definition of "a deck and its children"
        (Anki's deck list, ``deck:`` search, rename/delete scope).

        The parent is matched the way Anki does: ``decks.name`` is ``COLLATE
        unicase``, so plain ``=`` folds case (including non-ASCII); an explicit
        ``COLLATE NOCASE`` would downgrade that to ASCII. Descendants are then
        matched on the *stored* parent name followed by ``::`` using ``substr``
        rather than ``LIKE``: ``_`` and ``%`` in a deck name are literal, so
        ``A_B`` never claims ``AXB::child`` (#73).
        """
        wanted = name.strip()
        if not wanted:
            return None
        parent = conn.execute("SELECT name FROM decks WHERE name = ?", (wanted,)).fetchone()
        if parent is None:
            return None
        canonical = str(parent["name"])
        prefix = f"{canonical}::"
        rows = conn.execute(
            """
            SELECT id, name, kind
            FROM decks
            WHERE name = ? OR substr(name, 1, ?) = ?
            ORDER BY LENGTH(name), name
            """,
            (canonical, len(prefix), prefix),
        ).fetchall()
        return DeckSubtree(name=canonical, rows=rows)

    def _deck_filter(
        self,
        conn: sqlite3.Connection,
        deck: str | None,
    ) -> tuple[str, tuple[int, ...]]:
        """``AND did IN (...)`` for the deck and its children, like Anki's deck
        list and ``deck:`` search: ``--deck Japanese`` covers ``Japanese::Core``."""
        if deck is None:
            return "", ()

        found = self._deck_subtree(conn, deck)
        if found is None:
            # impossible clause
            return " AND did IN (-1)", ()

        ids = [int(row["id"]) for row in found.rows]
        placeholders = ", ".join(["?"] * len(ids))
        return f" AND did IN ({placeholders})", tuple(ids)

    def _filtered_deck_reschedules(self, conn: sqlite3.Connection, did: int) -> bool | None:
        """``None`` if ``did`` is not a filtered deck, else its ``reschedule`` flag."""
        row = conn.execute("SELECT kind FROM decks WHERE id = ?", (did,)).fetchone()
        if row is None:
            return None
        kind = self._decode_deck_kind(bytes(row["kind"] or b""), did=did)
        kind_name, kind_msg = betterproto.which_one_of(kind, "kind")
        if kind_name != "filtered" or kind_msg is None:
            return None
        return bool(kind_msg.reschedule)

    def _read_deck_config_map(
        self,
        conn: sqlite3.Connection,
    ) -> dict[int, dict[str, JSONValue]]:
        rows = conn.execute(
            """
            SELECT id, name, config
            FROM deck_config
            ORDER BY id
            """
        ).fetchall()

        out: dict[int, dict[str, JSONValue]] = {}
        for row in rows:
            dcid = int(row["id"])
            cfg = self._decode_deck_config(bytes(row["config"] or b""), dcid=dcid)
            out[dcid] = {
                "id": dcid,
                "name": str(row["name"]),
                "new_per_day": int(cfg.new_per_day),
                "reviews_per_day": int(cfg.reviews_per_day),
                "desired_retention": float(cfg.desired_retention),
            }
        return out

    def _deck_config_for_deck(
        self, conn: sqlite3.Connection, deck_id: int
    ) -> tuple[DeckConfigConfig, float | None, bool]:
        """The deck's options preset, its retention override, and whether a config row exists."""
        deck_row = conn.execute("SELECT kind FROM decks WHERE id = ?", (deck_id,)).fetchone()
        config_id = 1
        deck_retention: float | None = None
        if deck_row is not None:
            kind = self._decode_deck_kind(bytes(deck_row["kind"] or b""), did=deck_id)
            kind_name, kind_msg = betterproto.which_one_of(kind, "kind")
            if kind_name == "normal" and kind_msg is not None:
                config_id = int(kind_msg.config_id or 1)
                deck_retention = (
                    float(kind_msg.desired_retention)
                    if kind_msg.desired_retention is not None
                    else None
                )

        cfg_row = conn.execute(
            "SELECT config FROM deck_config WHERE id = ?",
            (config_id,),
        ).fetchone()
        cfg = (
            self._decode_deck_config(bytes(cfg_row["config"] or b""), dcid=config_id)
            if cfg_row is not None
            else DeckConfigConfig()
        )
        return cfg, deck_retention, cfg_row is not None
