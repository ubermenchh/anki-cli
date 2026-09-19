"""Cards: reads, due counts, and the queue mutators (suspend/bury/move/flag/
reschedule/reset), plus revlog reads.

The ``RESTORED_DUE_*`` / ``LEAVE_FILTERED_DECK_SQL`` fragments and
``queue_from_type_sql`` encode how a card leaves the suspended/buried/filtered
state; decks and scheduling import them from here.
"""

from __future__ import annotations

import sqlite3
import time

from anki_cli.core.due import LEARN_DUE_EPOCH_THRESHOLD
from anki_cli.db.deck_lookup import DeckLookupMixin
from anki_cli.db.search_sql import compile_card_query
from anki_cli.models.output import JSONValue

# A card parked in a filtered deck keeps its real due in ``odue`` while ``due``
# holds a position; rslib's restore_queue_after_bury_or_suspend reads
# ``original_due`` when it is set. Use this wherever the *scheduling* due of a
# card is needed regardless of where it currently lives.
RESTORED_DUE_SQL = "CASE WHEN odue > 0 THEN odue ELSE due END"

# Same, but only for a card that is actually on loan (rslib
# remove_from_filtered_deck_restoring_queue returns early when odid == 0, so a
# stray odue on a home-deck card must not rewrite its due).
RESTORED_DUE_IF_ON_LOAN_SQL = "CASE WHEN odid != 0 AND odue > 0 THEN odue ELSE due END"

# rslib Card::remove_from_filtered_deck_before_reschedule: a card that is about
# to be given a brand-new schedule (answer, forget, set due date) first goes
# home; the caller then writes queue/due itself.
LEAVE_FILTERED_DECK_SQL = "did = CASE WHEN odid != 0 THEN odid ELSE did END, odid = 0, odue = 0"


def queue_from_type_sql(*, due_expr: str = RESTORED_DUE_SQL) -> str:
    """SQL CASE recomputing ``queue`` from ``type`` (rslib ``restore_queue_from_type``).

    Used when a card leaves the suspended/buried/filtered state. ``due_expr`` is
    the expression holding the card's scheduling due *after* the surrounding
    UPDATE, which matters because SQLite evaluates SET clauses against the
    pre-update row. The default reads ``odue`` for cards in a filtered deck.
    """
    return f"""CASE
                        WHEN type = 0 THEN 0
                        WHEN type IN (1, 3) THEN
                            CASE WHEN ({due_expr}) > {LEARN_DUE_EPOCH_THRESHOLD} THEN 1 ELSE 3 END
                        ELSE 2
                    END"""


class CardsMixin(DeckLookupMixin):
    """Public card API."""

    def find_cards(self, query: str) -> list[int]:
        compiled = compile_card_query(query, ctx=self._search_context())

        joins_sql = ""
        if compiled.joins:
            joins_sql = "\n            " + "\n            ".join(compiled.joins)

        sql = f"""
            SELECT DISTINCT c.id
            FROM cards AS c{joins_sql}
            WHERE {compiled.where}
            ORDER BY c.id
        """

        with self._connect() as conn:
            rows = conn.execute(sql, compiled.params).fetchall()
        return [int(row["id"]) for row in rows]

    def get_card(self, card_id: int) -> dict[str, JSONValue]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    c.id, c.nid, c.did, c.ord, c.mod, c.usn, c.type, c.queue, c.due,
                    c.ivl, c.factor, c.reps, c.lapses, c.left, c.odue, c.odid,
                    c.flags, c.data,
                    n.mid AS note_mid,
                    nt.name AS notetype_name,
                    n.flds AS note_fields, n.tags AS note_tags,
                    d.name AS deck_name
                FROM cards AS c
                JOIN notes AS n ON n.id = c.nid
                LEFT JOIN notetypes AS nt ON nt.id = n.mid
                LEFT JOIN decks AS d ON d.id = c.did
                WHERE c.id = ?
                """,
                (card_id,),
            ).fetchone()

            timing = self._timing(conn, int(time.time())) if row is not None else None

        if row is None:
            raise LookupError(f"Card not found: {card_id}")

        card_type = int(row["type"])
        queue = int(row["queue"])
        due_raw = int(row["due"])
        left_raw = int(row["left"])
        data_raw = str(row["data"] or "")

        return {
            "cardId": int(row["id"]),
            "note": int(row["nid"]),
            "deckId": int(row["did"]),
            "deckName": str(row["deck_name"] or ""),
            "ord": int(row["ord"]),
            "type": int(row["type"]),
            "queue": int(row["queue"]),
            "due": int(row["due"]),
            "interval": int(row["ivl"]),
            "factor": int(row["factor"]),
            "reps": int(row["reps"]),
            "lapses": int(row["lapses"]),
            "left": int(row["left"]),
            "flags": int(row["flags"]),
            "fields": self._split_fields(str(row["note_fields"] or "")),
            "tags": self._parse_tags(str(row["note_tags"] or "")),
            "data": str(row["data"] or ""),
            "notetype_id": int(row["note_mid"]),
            "notetype_name": str(row["notetype_name"] or ""),
            "due_info": self._decode_due(
                card_type=card_type,
                queue=queue,
                due_raw=due_raw,
                timing=timing,
            ),
            "left_info": self._decode_left(left_raw),
            "data_parsed": self._parse_card_data(data_raw),
        }

    def get_due_counts(self, deck: str | None = None) -> dict[str, int]:
        now_sec = int(time.time())

        with self._connect() as conn:
            today_days = self._timing(conn, now_sec).days_elapsed
            did_filter, params = self._deck_filter(conn, deck)

            new_count = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM cards WHERE queue = 0 {did_filter}",
                    params,
                ).fetchone()[0]
            )
            # queue 1 stores an epoch, queue 3 (day-learn) a day index.
            learn_count = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) FROM cards
                    WHERE ((queue = 1 AND due <= ?) OR (queue = 3 AND due <= ?)) {did_filter}
                    """,
                    (now_sec, today_days, *params),
                ).fetchone()[0]
            )
            review_count = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM cards WHERE queue = 2 AND due <= ? {did_filter}",
                    (today_days, *params),
                ).fetchone()[0]
            )

        return {
            "new": new_count,
            "learn": learn_count,
            "review": review_count,
            "total": new_count + learn_count + review_count,
        }

    def get_next_due_card(self, deck: str | None = None) -> dict[str, JSONValue]:
        now_sec = int(time.time())

        with self._connect() as conn:
            timing = self._timing(conn, now_sec)
            today_days = timing.days_elapsed
            did_filter, params = self._deck_filter(conn, deck)

            # 1) learning/relearning due. Intraday (queue 1) holds an epoch,
            #    day-learn (queue 3) a day index; order both by absolute time.
            day0_epoch = timing.day_start_epoch(0)
            row = conn.execute(
                f"""
                SELECT id, due
                FROM cards
                WHERE ((queue = 1 AND due <= ?) OR (queue = 3 AND due <= ?)) {did_filter}
                ORDER BY CASE WHEN queue = 1 THEN due ELSE ? + due * 86400 END ASC, id ASC
                LIMIT 1
                """,
                (now_sec, today_days, *params, day0_epoch),
            ).fetchone()
            if row is not None:
                return {"card_id": int(row["id"]), "kind": "learn_due"}

            # 2) review due (day index)
            row = conn.execute(
                f"""
                SELECT id, due
                FROM cards
                WHERE queue = 2 AND due <= ? {did_filter}
                ORDER BY due ASC, id ASC
                LIMIT 1
                """,
                (today_days, *params),
            ).fetchone()
            if row is not None:
                return {"card_id": int(row["id"]), "kind": "review_due"}

            # 3) new (position)
            row = conn.execute(
                f"""
                SELECT id, due
                FROM cards
                WHERE queue = 0 {did_filter}
                ORDER BY due ASC, id ASC
                LIMIT 1
                """,
                params,
            ).fetchone()
            if row is not None:
                return {"card_id": int(row["id"]), "kind": "new"}

        return {"card_id": None, "kind": "none"}

    def move_cards(self, card_ids: list[int], deck: str) -> dict[str, JSONValue]:
        ids = sorted({int(cid) for cid in card_ids if int(cid) > 0})
        if not ids:
            return {"moved": 0, "card_ids": []}
        with self._connect_write() as conn:
            did = self._resolve_deck_id(conn, deck)
            if self._filtered_deck_reschedules(conn, did) is not None:
                # rslib FilteredDeckError::CanNotMoveCardsInto
                raise ValueError(f"Cannot move cards into a filtered deck: {deck}")
            placeholders = ", ".join(["?"] * len(ids))
            # A card leaving a filtered deck first restores its real schedule
            # (rslib Card::set_deck -> remove_from_filtered_deck_restoring_queue).
            updated = conn.execute(
                f"""
                UPDATE cards
                SET due = {RESTORED_DUE_IF_ON_LOAN_SQL},
                    queue = CASE
                        WHEN odid = 0 OR queue < 0 THEN queue
                        ELSE {queue_from_type_sql()}
                    END,
                    odid = 0,
                    odue = 0,
                    did = ?,
                    mod = ?,
                    usn = -1
                WHERE id IN ({placeholders})
                """,
                (did, int(time.time()), *ids),
            ).rowcount
        return {"moved": int(updated), "card_ids": ids, "deck": deck}

    def set_card_flag(self, card_ids: list[int], flag: int) -> dict[str, JSONValue]:
        if flag < 0 or flag > 7:
            raise ValueError("flag must be in range 0..7")
        ids = sorted({int(cid) for cid in card_ids if int(cid) > 0})
        if not ids:
            return {"updated": 0, "card_ids": []}
        with self._connect_write() as conn:
            placeholders = ", ".join(["?"] * len(ids))
            updated = conn.execute(
                f"UPDATE cards SET flags = ?, mod = ?, usn = -1 WHERE id IN ({placeholders})",
                (flag, int(time.time()), *ids),
            ).rowcount
        return {"updated": int(updated), "card_ids": ids, "flag": flag}

    def bury_cards(self, card_ids: list[int]) -> dict[str, JSONValue]:
        ids = sorted({int(cid) for cid in card_ids if int(cid) > 0})
        if not ids:
            return {"buried": 0, "card_ids": []}
        with self._connect_write() as conn:
            placeholders = ", ".join(["?"] * len(ids))
            updated = conn.execute(
                f"UPDATE cards SET queue = -2, mod = ?, usn = -1 WHERE id IN ({placeholders})",
                (int(time.time()), *ids),
            ).rowcount
        return {"buried": int(updated), "card_ids": ids}

    def unbury_cards(self, deck: str | None = None) -> dict[str, JSONValue]:
        with self._connect_write() as conn:
            now_sec = int(time.time())
            if deck is None:
                updated = conn.execute(
                    f"""
                    UPDATE cards
                    SET queue = {queue_from_type_sql()},
                    mod = ?, usn = -1
                    WHERE queue IN (-2, -3)
                    """,
                    (now_sec,),
                ).rowcount
                return {"unburied": int(updated), "scope": "all"}

            found = self._deck_subtree(conn, deck)
            if found is None:
                return {"unburied": 0, "deck": deck}
            dids = [int(r["id"]) for r in found.rows]

            placeholders = ", ".join(["?"] * len(dids))
            updated = conn.execute(
                f"""
                UPDATE cards
                SET queue = {queue_from_type_sql()},
                mod = ?, usn = -1
                WHERE queue IN (-2, -3) AND did IN ({placeholders})
                """,
                (now_sec, *dids),
            ).rowcount
            return {"unburied": int(updated), "deck": deck}

    def reschedule_cards(self, card_ids: list[int], days: int) -> dict[str, JSONValue]:
        if days < 0:
            raise ValueError("days must be >= 0")
        ids = sorted({int(cid) for cid in card_ids if int(cid) > 0})
        if not ids:
            return {"rescheduled": 0, "card_ids": []}

        with self._connect_write() as conn:
            today = self._timing(conn, int(time.time())).days_elapsed
            target_due = today + days
            placeholders = ", ".join(["?"] * len(ids))
            updated = conn.execute(
                f"""
                UPDATE cards
                SET type = 2, queue = 2, due = ?, ivl = ?, {LEAVE_FILTERED_DECK_SQL},
                    mod = ?, usn = -1
                WHERE id IN ({placeholders})
                """,
                (target_due, max(1, days), int(time.time()), *ids),
            ).rowcount
        return {"rescheduled": int(updated), "card_ids": ids, "days": days}

    def reset_cards(self, card_ids: list[int]) -> dict[str, JSONValue]:
        ids = sorted({int(cid) for cid in card_ids if int(cid) > 0})
        if not ids:
            return {"reset": 0, "card_ids": []}

        with self._connect_write() as conn:
            next_due = self._next_new_due(conn)
            now_sec = int(time.time())
            updated = 0
            for offset, cid in enumerate(ids):
                changed = conn.execute(
                    f"""
                    UPDATE cards
                    SET type = 0, queue = 0, due = ?, ivl = 0, factor = 0, reps = 0, lapses = 0,
                        left = 0, {LEAVE_FILTERED_DECK_SQL}, data = '{{}}', mod = ?, usn = -1
                    WHERE id = ?
                    """,
                    (next_due + offset, now_sec, cid),
                ).rowcount
                updated += int(changed)
        return {"reset": updated, "card_ids": ids}

    def delete_card(self, card_id: int) -> dict[str, JSONValue]:
        if card_id <= 0:
            return {"card_id": card_id, "deleted": False}

        with self._connect_write() as conn:
            row = conn.execute("SELECT id FROM cards WHERE id = ?", (card_id,)).fetchone()
            if row is None:
                return {"card_id": card_id, "deleted": False}

            deleted = int(
                conn.execute("DELETE FROM cards WHERE id = ?", (card_id,)).rowcount
            )
            self._insert_graves(conn, [card_id], grave_type=0)

        return {"card_id": card_id, "deleted": deleted > 0}

    def suspend_cards(self, card_ids: list[int]) -> dict[str, JSONValue]:
        return self._set_cards_suspended(card_ids, suspended=True)

    def unsuspend_cards(self, card_ids: list[int]) -> dict[str, JSONValue]:
        return self._set_cards_suspended(card_ids, suspended=False)

    def _set_cards_suspended(
        self,
        card_ids: list[int],
        *,
        suspended: bool,
    ) -> dict[str, JSONValue]:
        normalized_ids = sorted({int(cid) for cid in card_ids if int(cid) > 0})
        if not normalized_ids:
            return {"updated": 0, "card_ids": []}

        with self._connect_write() as conn:
            placeholders = ", ".join(["?"] * len(normalized_ids))
            existing_rows = conn.execute(
                f"SELECT id FROM cards WHERE id IN ({placeholders})",
                tuple(normalized_ids),
            ).fetchall()
            existing_ids = [int(row["id"]) for row in existing_rows]
            if not existing_ids:
                return {"updated": 0, "card_ids": []}

            existing_placeholders = ", ".join(["?"] * len(existing_ids))
            now_sec = int(time.time())
            if suspended:
                conn.execute(
                    (
                        "UPDATE cards SET queue = -1, mod = ?, usn = -1 "
                        f"WHERE id IN ({existing_placeholders})"
                    ),
                    (now_sec, *existing_ids),
                )
                return {
                    "updated": len(existing_ids),
                    "suspended": len(existing_ids),
                    "card_ids": existing_ids,
                }

            conn.execute(
                f"""
                UPDATE cards
                SET
                    queue = {queue_from_type_sql()},
                    mod = ?,
                    usn = -1
                WHERE id IN ({existing_placeholders})
                """,
                (now_sec, *existing_ids),
            )
            return {
                "updated": len(existing_ids),
                "unsuspended": len(existing_ids),
                "card_ids": existing_ids,
            }

    def _get_next_due_for_deck(self, deck_name: str) -> dict[str, JSONValue] | None:
        with self._connect() as conn:
            deck_row = conn.execute(
                "SELECT id FROM decks WHERE name = ?",
                (deck_name,),
            ).fetchone()
            if deck_row is None:
                return None
            did = int(deck_row["id"])
            timing = self._timing(conn, int(time.time()))
            # Only queue 1 stores an epoch; queues 2 and 3 store a day index.
            row = conn.execute(
                """
                SELECT queue, due
                FROM cards
                WHERE did = ? AND queue IN (1, 2, 3)
                ORDER BY CASE WHEN queue = 1 THEN due ELSE ? + due * 86400 END, id
                LIMIT 1
                """,
                (did, timing.day_start_epoch(0)),
            ).fetchone()
            if row is None:
                return None

        queue = int(row["queue"])
        due = int(row["due"])
        if queue == 1:
            return {"queue": queue, "epoch_secs": due}
        return {"queue": queue, "day_index": due, "epoch_secs": timing.day_start_epoch(due)}

    @staticmethod
    def _scheduling_due(row: sqlite3.Row) -> int:
        """The card's real due: ``odue`` while parked in a filtered deck, else ``due``."""
        keys = row.keys()
        if "odid" in keys and "odue" in keys and int(row["odid"]) != 0 and int(row["odue"]) > 0:
            return int(row["odue"])
        return int(row["due"])

    def get_revlog(self, card_id: int, limit: int = 50) -> list[dict[str, JSONValue]]:
        bounded_limit = max(1, min(limit, 1000))

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, cid, usn, ease, ivl, lastIvl, factor, time, type
                FROM revlog
                WHERE cid = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (card_id, bounded_limit),
            ).fetchall()

        return [self._revlog_row_to_item(row) for row in rows]

    def _revlog_row_to_item(self, row: sqlite3.Row) -> dict[str, JSONValue]:
        review_id = int(row["id"])
        interval_raw = int(row["ivl"])
        last_interval_raw = int(row["lastIvl"])
        factor_raw = int(row["factor"])
        review_type = int(row["type"])

        return {
            "id": review_id,
            "card_id": int(row["cid"]),
            "usn": int(row["usn"]),
            "ease": int(row["ease"]),
            "review_type": review_type,
            "review_type_name": self._revlog_type_name(review_type),
            "duration_ms": int(row["time"]),
            "reviewed_at_epoch_ms": review_id,
            "reviewed_at_epoch_secs": review_id // 1000,
            "interval": self._decode_revlog_interval(interval_raw),
            "last_interval": self._decode_revlog_interval(last_interval_raw),
            "factor": factor_raw,
            "factor_info": self._decode_revlog_factor(factor_raw),
        }

    def _decode_revlog_interval(self, value: int) -> dict[str, JSONValue]:
        # Anki convention: negative => seconds, positive => days.
        if value < 0:
            seconds = abs(value)
            return {
                "raw": value,
                "unit": "seconds",
                "seconds": seconds,
                "days": None,
            }

        return {
            "raw": value,
            "unit": "days",
            "days": value,
            "seconds": None,
        }

    def _decode_revlog_factor(self, factor: int) -> dict[str, JSONValue]:
        # FSRS review log uses roughly 100..1100 as difficulty*100.
        if 100 <= factor <= 1100:
            return {
                "raw": factor,
                "model": "fsrs_difficulty",
                "difficulty": factor / 100.0,
                "ease_multiplier": None,
            }

        # SM-2 style ease factor permille (eg 2500 => 2.5).
        if factor > 0:
            return {
                "raw": factor,
                "model": "sm2_ease_permille",
                "difficulty": None,
                "ease_multiplier": factor / 1000.0,
            }

        return {
            "raw": factor,
            "model": "unknown",
            "difficulty": None,
            "ease_multiplier": None,
        }

    def _revlog_type_name(self, review_type: int) -> str:
        return {
            0: "learn",
            1: "review",
            2: "relearn",
            3: "filtered",
            4: "manual",
        }.get(review_type, "unknown")
