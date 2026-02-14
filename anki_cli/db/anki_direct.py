from __future__ import annotations

import shlex
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from anki_cli.backends.protocol import JSONValue


class AnkiDirectReadStore:
    """Read-only helpers for Anki's collection.anki21b schema."""

    def __init__(self, db_path: Path) -> None:
        resolved = db_path.expanduser().resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"Direct DB not found: {resolved}")
        self.db_path = resolved

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=1.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        try:
            yield conn
        finally:
            conn.close()

    # ---- deck / notetype -------------------------------------------------

    def get_decks(self) -> list[dict[str, JSONValue]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name FROM decks ORDER BY LOWER(name), id"
            ).fetchall()

        return [{"id": int(row["id"]), "name": str(row["name"])} for row in rows]

    def get_notetypes(self) -> list[dict[str, JSONValue]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name FROM notetypes ORDER BY LOWER(name), id"
            ).fetchall()

        return [
            {
                "id": int(row["id"]),
                "name": str(row["name"]),
                # Full field/template decoding requires protobuf stage.
                "field_count": 0,
                "template_count": 0,
                "fields": [],
                "templates": [],
            }
            for row in rows
        ]

    def get_notetype(self, name: str) -> dict[str, JSONValue]:
        normalized = name.strip()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, name, LENGTH(config) AS config_len FROM notetypes WHERE name = ?",
                (normalized,),
            ).fetchone()

        if row is None:
            raise LookupError(f"Notetype not found: {normalized}")

        return {
            "id": int(row["id"]),
            "name": str(row["name"]),
            "fields": [],
            "templates": {},
            "config_blob_bytes": int(row["config_len"] or 0),
        }

    # ---- notes ------------------------------------------------------------

    def find_note_ids(self, query: str) -> list[int]:
        clauses, params, joins = self._note_query_to_sql(query)

        sql = f"""
            SELECT DISTINCT n.id
            FROM notes AS n
            {joins}
            WHERE {" AND ".join(clauses)}
            ORDER BY n.id
        """

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [int(row["id"]) for row in rows]

    def get_note(self, note_id: int) -> dict[str, JSONValue]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, guid, mid, mod, usn, tags, flds, sfld, csum, flags, data
                FROM notes
                WHERE id = ?
                """,
                (note_id,),
            ).fetchone()

        if row is None:
            raise LookupError(f"Note not found: {note_id}")

        raw_tags = str(row["tags"] or "")
        raw_fields = str(row["flds"] or "")

        return {
            "id": int(row["id"]),
            "guid": str(row["guid"]),
            "mid": int(row["mid"]),
            "mod": int(row["mod"]),
            "usn": int(row["usn"]),
            "tags": self._parse_tags(raw_tags),
            "fields": self._split_fields(raw_fields),
            "sfld": row["sfld"],
            "csum": int(row["csum"]),
            "flags": int(row["flags"]),
            "data": str(row["data"] or ""),
        }

    def get_tags(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT tags FROM notes").fetchall()

        tags: set[str] = set()
        for row in rows:
            tags.update(self._parse_tags(str(row["tags"] or "")))
        return sorted(tags, key=str.lower)

    # ---- cards ------------------------------------------------------------

    def find_card_ids(self, query: str) -> list[int]:
        clauses, params, joins = self._card_query_to_sql(query)

        sql = f"""
            SELECT DISTINCT c.id
            FROM cards AS c
            JOIN notes AS n ON n.id = c.nid
            {joins}
            WHERE {" AND ".join(clauses)}
            ORDER BY c.id
        """

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [int(row["id"]) for row in rows]

    def get_card(self, card_id: int) -> dict[str, JSONValue]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    c.id, c.nid, c.did, c.ord, c.mod, c.usn, c.type, c.queue, c.due,
                    c.ivl, c.factor, c.reps, c.lapses, c.left, c.odue, c.odid,
                    c.flags, c.data,
                    n.flds AS note_fields, n.tags AS note_tags,
                    d.name AS deck_name
                FROM cards AS c
                JOIN notes AS n ON n.id = c.nid
                LEFT JOIN decks AS d ON d.id = c.did
                WHERE c.id = ?
                """,
                (card_id,),
            ).fetchone()

        if row is None:
            raise LookupError(f"Card not found: {card_id}")

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
        }

    def get_due_counts(self, deck: str | None = None) -> dict[str, int]:
        now_sec = int(time.time())
        today_days = self._today_due_index(now_sec)

        with self._connect() as conn:
            did_filter, params = self._deck_filter(conn, deck)

            new_count = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM cards WHERE queue = 0 {did_filter}",
                    params,
                ).fetchone()[0]
            )
            learn_count = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM cards WHERE queue IN (1, 3) AND due <= ? {did_filter}",
                    (now_sec, *params),
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

    # ---- SQL query helpers ------------------------------------------------

    def _note_query_to_sql(self, query: str) -> tuple[list[str], list[JSONValue], str]:
        tokens = self._tokenize(query)
        clauses = ["1=1"]
        params: list[JSONValue] = []
        joins = "LEFT JOIN cards AS c ON c.nid = n.id LEFT JOIN decks AS d ON d.id = c.did"

        for token in tokens:
            low = token.lower()
            if low.startswith("nid:"):
                nid = self._parse_int_token(token, "nid:")
                clauses.append("n.id = ?")
                params.append(nid)
            elif low.startswith("tag:"):
                tag = self._strip_quotes(token[4:])
                clauses.append("n.tags LIKE ?")
                params.append(f"% {tag} %")
            elif low.startswith("deck:"):
                deck = self._deck_like_value(token[5:])
                clauses.append("d.name LIKE ?")
                params.append(deck)
            elif low.startswith("added:"):
                days = self._parse_int_token(token, "added:")
                cutoff = int(time.time()) - (days * 86400)
                clauses.append("n.mod >= ?")
                params.append(cutoff)
            else:
                clauses.append("n.flds LIKE ?")
                params.append(f"%{token}%")

        return clauses, params, joins

    def _card_query_to_sql(self, query: str) -> tuple[list[str], list[JSONValue], str]:
        tokens = self._tokenize(query)
        clauses = ["1=1"]
        params: list[JSONValue] = []
        joins = "LEFT JOIN decks AS d ON d.id = c.did"

        now_sec = int(time.time())
        due_day_index = self._today_due_index(now_sec)

        for token in tokens:
            low = token.lower()
            if low.startswith("cid:"):
                cid = self._parse_int_token(token, "cid:")
                clauses.append("c.id = ?")
                params.append(cid)
            elif low.startswith("nid:"):
                nid = self._parse_int_token(token, "nid:")
                clauses.append("c.nid = ?")
                params.append(nid)
            elif low.startswith("tag:"):
                tag = self._strip_quotes(token[4:])
                clauses.append("n.tags LIKE ?")
                params.append(f"% {tag} %")
            elif low.startswith("deck:"):
                deck = self._deck_like_value(token[5:])
                clauses.append("d.name LIKE ?")
                params.append(deck)
            elif low.startswith("added:"):
                days = self._parse_int_token(token, "added:")
                cutoff = int(time.time()) - (days * 86400)
                clauses.append("n.mod >= ?")
                params.append(cutoff)
            elif low == "is:new":
                clauses.append("c.queue = 0")
            elif low == "is:learn":
                clauses.append("c.queue IN (1, 3)")
            elif low == "is:review":
                clauses.append("c.queue = 2")
            elif low == "is:suspended":
                clauses.append("c.queue = -1")
            elif low == "is:due":
                clauses.append(
                    "("
                    "c.queue = 0 OR "
                    "(c.queue IN (1, 3) AND c.due <= ?) OR "
                    "(c.queue = 2 AND c.due <= ?)"
                    ")"
                )
                params.append(now_sec)
                params.append(due_day_index)
            else:
                clauses.append("n.flds LIKE ?")
                params.append(f"%{token}%")

        return clauses, params, joins

    # ---- low-level helpers ------------------------------------------------

    def _today_due_index(self, now_sec: int) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT crt FROM col LIMIT 1").fetchone()

        if row is None:
            return int(now_sec // 86400)

        crt_sec = int(row["crt"])
        return max(0, int(now_sec // 86400) - int(crt_sec // 86400))

    def _deck_filter(
        self,
        conn: sqlite3.Connection,
        deck: str | None,
    ) -> tuple[str, tuple[int, ...]]:
        if deck is None:
            return "", ()

        rows = conn.execute("SELECT id FROM decks WHERE name = ?", (deck.strip(),)).fetchall()
        ids = [int(row["id"]) for row in rows]
        if not ids:
            # impossible clause
            return " AND did IN (-1)", ()

        placeholders = ", ".join(["?"] * len(ids))
        return f" AND did IN ({placeholders})", tuple(ids)

    def _tokenize(self, query: str) -> list[str]:
        raw = query.strip()
        if not raw:
            return []
        try:
            return shlex.split(raw)
        except ValueError:
            return [raw]

    def _parse_int_token(self, token: str, prefix: str) -> int:
        raw = token[len(prefix) :].strip()
        return int(self._strip_quotes(raw))

    def _strip_quotes(self, value: str) -> str:
        out = value.strip()
        if (out.startswith('"') and out.endswith('"')) or (
            out.startswith("'") and out.endswith("'")
        ):
            return out[1:-1]
        return out

    def _deck_like_value(self, raw: str) -> str:
        deck = self._strip_quotes(raw)
        return deck.replace("*", "%")

    def _split_fields(self, value: str) -> list[str]:
        return value.split("\x1f") if value else []

    def _parse_tags(self, value: str) -> list[str]:
        stripped = value.strip()
        if not stripped:
            return []
        return [part for part in stripped.split(" ") if part]