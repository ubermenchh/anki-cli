from __future__ import annotations

import json
import shlex
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha1
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import betterproto
from fsrs import Card as FSRSCard
from fsrs import Rating, Scheduler, State

if TYPE_CHECKING:
    from anki_cli.backends.protocol import JSONValue
else:
    JSONValue = Any
from anki_cli.proto.anki.deck_config import DeckConfigConfig
from anki_cli.proto.anki.decks import DeckCommon, DeckKindContainer
from anki_cli.proto.anki.notetypes import (
    NotetypeConfig,
    NotetypeFieldConfig,
    NotetypeTemplateConfig,
)


class AnkiDirectReadStore:
    """Helpers for Anki's collection(.anki21b/.anki2) schema."""

    def __init__(self, db_path: Path) -> None:
        resolved = db_path.expanduser().resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"Direct DB not found: {resolved}")
        self.db_path = resolved

    @staticmethod
    def _unicase_collation(left: str | None, right: str | None) -> int:
        lval = (left or "").casefold()
        rval = (right or "").casefold()
        return (lval > rval) - (lval < rval)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=1.0)
        conn.row_factory = sqlite3.Row
        conn.create_collation("unicase", self._unicase_collation)
        conn.execute("PRAGMA query_only = ON")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _connect_write(self) -> Iterator[sqlite3.Connection]:
        self._ensure_write_safe()
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.create_collation("unicase", self._unicase_collation)
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ---- deck / notetype -------------------------------------------------

    def get_decks(self) -> list[dict[str, JSONValue]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, name, common, kind
                FROM decks
                ORDER BY LOWER(name), id
                """
            ).fetchall()
            deck_config_map = self._read_deck_config_map(conn)

        output: list[dict[str, JSONValue]] = []
        for row in rows:
            did = int(row["id"])
            name = str(row["name"])
            common = self._decode_deck_common(bytes(row["common"] or b""), did=did)
            kind = self._decode_deck_kind(bytes(row["kind"] or b""), did=did)

            kind_name, kind_msg = betterproto.which_one_of(kind, "kind")
            item: dict[str, JSONValue] = {
                "id": did,
                "name": name,
                "kind": kind_name or "unknown",
                "stats": {
                    "new_studied": int(common.new_studied),
                    "review_studied": int(common.review_studied),
                    "learning_studied": int(common.learning_studied),
                },
            }

            if kind_name == "normal" and kind_msg is not None:
                config_id = int(kind_msg.config_id)
                item["config_id"] = config_id
                item["description"] = str(kind_msg.description or "")
                item["new_limit"] = (
                    int(kind_msg.new_limit) if kind_msg.new_limit is not None else None
                )
                item["review_limit"] = (
                    int(kind_msg.review_limit) if kind_msg.review_limit is not None else None
                )
                if config_id in deck_config_map:
                    item["config"] = deck_config_map[config_id]

            elif kind_name == "filtered" and kind_msg is not None:
                item["search_terms"] = [term.search for term in kind_msg.search_terms]
                item["reschedule"] = bool(kind_msg.reschedule)

            output.append(item)

        return output

    def get_notetypes(self) -> list[dict[str, JSONValue]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, name
                FROM notetypes
                ORDER BY LOWER(name), id
                """
            ).fetchall()
            fields_by_ntid, templates_by_ntid = self._load_notetype_parts(conn)

        result: list[dict[str, JSONValue]] = []
        for row in rows:
            ntid = int(row["id"])
            fields = fields_by_ntid.get(ntid, [])
            templates = templates_by_ntid.get(ntid, [])

            result.append(
                {
                    "id": ntid,
                    "name": str(row["name"]),
                    "field_count": len(fields),
                    "template_count": len(templates),
                    "fields": [str(item["name"]) for item in fields],
                    "templates": [str(item["name"]) for item in templates],
                }
            )
        return result

    def get_notetype(self, name: str) -> dict[str, JSONValue]:
        normalized = name.strip()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, name, config
                FROM notetypes
                WHERE name = ?
                """,
                (normalized,),
            ).fetchone()

            if row is None:
                raise LookupError(f"Notetype not found: {normalized}")

            ntid = int(row["id"])
            fields_by_ntid, templates_by_ntid = self._load_notetype_parts(conn)

        config = self._decode_notetype_config(bytes(row["config"] or b""), ntid=ntid)
        fields = fields_by_ntid.get(ntid, [])
        templates = templates_by_ntid.get(ntid, [])

        templates_map: dict[str, JSONValue] = {
            str(item["name"]): {
                "Front": str(item["qfmt"]),
                "Back": str(item["afmt"]),
                "ord": self._coerce_int_value(item.get("ord")) or 0,
            }
            for item in templates
        }

        kind = "cloze" if int(config.kind) == 1 else "normal"

        return {
            "id": ntid,
            "name": str(row["name"]),
            "kind": kind,
            "sort_field_idx": int(config.sort_field_idx),
            "fields": [str(item["name"]) for item in fields],
            "templates": templates_map,
            "styling": {"css": config.css},
            "requirements": [
                {
                    "card_ord": int(req.card_ord),
                    "kind": int(req.kind),
                    "field_ords": [int(x) for x in req.field_ords],
                }
                for req in config.reqs
            ],
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
                    (SELECT crt FROM col LIMIT 1) AS col_crt,
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

        card_type = int(row["type"])
        queue = int(row["queue"])
        due_raw = int(row["due"])
        left_raw = int(row["left"])
        data_raw = str(row["data"] or "")
        col_crt_raw = row["col_crt"]
        col_crt_sec = int(col_crt_raw) if col_crt_raw is not None else None

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
            "due_info": self._decode_due(
                card_type=card_type,
                queue=queue,
                due_raw=due_raw,
                col_crt_sec=col_crt_sec
            ),
            "left_info": self._decode_left(left_raw),
            "data_parsed": self._parse_card_data(data_raw),
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

    # ---- write paths ------------------------------------------------------

    def create_deck(self, name: str) -> dict[str, JSONValue]:
        return self.write_deck(name=name)

    def write_deck(
        self,
        *,
        name: str,
        deck_id: int | None = None,
        config_id: int | None = None,
        description: str | None = None,
    ) -> dict[str, JSONValue]:
        normalized = name.strip()
        if not normalized:
            raise ValueError("Deck name cannot be empty.")

        with self._connect_write() as conn:
            target_row: sqlite3.Row | None = None
            if deck_id is not None:
                target_row = conn.execute(
                    "SELECT id, common, kind FROM decks WHERE id = ?",
                    (deck_id,),
                ).fetchone()
                if target_row is None:
                    raise LookupError(f"Deck not found: {deck_id}")
            else:
                target_row = conn.execute(
                    "SELECT id, common, kind FROM decks WHERE name = ?",
                    (normalized,),
                ).fetchone()

            template = conn.execute(
                "SELECT common, kind FROM decks WHERE name = ? LIMIT 1",
                ("Default",),
            ).fetchone()
            if template is None:
                template = conn.execute(
                    "SELECT common, kind FROM decks ORDER BY id LIMIT 1"
                ).fetchone()
            if template is None:
                raise RuntimeError("No deck template row available to create a deck.")

            common_blob = (
                bytes(target_row["common"])
                if target_row is not None
                else bytes(template["common"])
            )
            kind_blob = (
                bytes(target_row["kind"])
                if target_row is not None
                else bytes(template["kind"])
            )
            decode_did = int(target_row["id"]) if target_row else -1
            deck_common = self._decode_deck_common(common_blob, did=decode_did)
            deck_kind = self._decode_deck_kind(kind_blob, did=decode_did)

            kind_name, kind_msg = betterproto.which_one_of(deck_kind, "kind")
            if kind_name == "normal" and kind_msg is not None:
                if config_id is not None:
                    kind_msg.config_id = int(config_id)
                if description is not None:
                    kind_msg.description = description
            elif kind_name == "":
                from anki_cli.proto.anki.decks import DeckNormal

                deck_kind.normal = DeckNormal(
                    config_id=int(config_id or 1),
                    description=description or "",
                )

            now_sec = int(time.time())
            if target_row is None:
                assigned_id = self._allocate_row_id(conn, "decks")
                conn.execute(
                    """
                    INSERT INTO decks (id, name, mtime_secs, usn, common, kind)
                    VALUES (?, ?, ?, -1, ?, ?)
                    """,
                    (
                        assigned_id,
                        normalized,
                        now_sec,
                        bytes(deck_common),
                        bytes(deck_kind),
                    ),
                )
                return {"deck": normalized, "id": assigned_id, "created": True}

            assigned_id = int(target_row["id"])
            conn.execute(
                """
                UPDATE decks
                SET name = ?, mtime_secs = ?, usn = -1, common = ?, kind = ?
                WHERE id = ?
                """,
                (
                    normalized,
                    now_sec,
                    bytes(deck_common),
                    bytes(deck_kind),
                    assigned_id,
                ),
            )
            return {"deck": normalized, "id": assigned_id, "created": False, "updated": True}

    def delete_deck(self, name: str) -> dict[str, JSONValue]:
        normalized = name.strip()
        if not normalized:
            raise ValueError("Deck name cannot be empty.")

        with self._connect_write() as conn:
            deck_rows = conn.execute(
                """
                SELECT id, name
                FROM decks
                WHERE name = ? OR name LIKE ?
                ORDER BY id
                """,
                (normalized, f"{normalized}::%"),
            ).fetchall()
            if not deck_rows:
                return {
                    "deck": normalized,
                    "deleted": False,
                    "deleted_decks": 0,
                    "deleted_notes": 0,
                    "deleted_cards": 0,
                }

            deck_ids = [int(row["id"]) for row in deck_rows]
            deck_placeholders = ", ".join(["?"] * len(deck_ids))

            card_rows = conn.execute(
                f"SELECT id, nid FROM cards WHERE did IN ({deck_placeholders})",
                tuple(deck_ids),
            ).fetchall()
            card_ids = [int(row["id"]) for row in card_rows]

            target_note_ids = sorted({int(row["nid"]) for row in card_rows})
            note_ids_to_delete: list[int] = []
            if target_note_ids:
                note_placeholders = ", ".join(["?"] * len(target_note_ids))
                membership = conn.execute(
                    f"""
                    SELECT
                        c.nid AS nid,
                        COUNT(*) AS total_cards,
                        SUM(CASE WHEN c.did IN ({deck_placeholders}) THEN 1 ELSE 0 END)
                            AS in_scope_cards
                    FROM cards AS c
                    WHERE c.nid IN ({note_placeholders})
                    GROUP BY c.nid
                    """,
                    (*deck_ids, *target_note_ids),
                ).fetchall()
                for row in membership:
                    if int(row["total_cards"]) == int(row["in_scope_cards"]):
                        note_ids_to_delete.append(int(row["nid"]))

            deleted_cards = 0
            if deck_ids:
                deleted_cards = int(
                    conn.execute(
                        f"DELETE FROM cards WHERE did IN ({deck_placeholders})",
                        tuple(deck_ids),
                    ).rowcount
                )

            deleted_notes = 0
            if note_ids_to_delete:
                note_placeholders = ", ".join(["?"] * len(note_ids_to_delete))
                deleted_notes = int(
                    conn.execute(
                        f"DELETE FROM notes WHERE id IN ({note_placeholders})",
                        tuple(note_ids_to_delete),
                    ).rowcount
                )

            deleted_decks = int(
                conn.execute(
                    f"DELETE FROM decks WHERE id IN ({deck_placeholders})",
                    tuple(deck_ids),
                ).rowcount
            )

            self._insert_graves(conn, card_ids, grave_type=0)
            self._insert_graves(conn, note_ids_to_delete, grave_type=1)
            self._insert_graves(conn, deck_ids, grave_type=2)

        return {
            "deck": normalized,
            "deleted": deleted_decks > 0,
            "deleted_decks": deleted_decks,
            "deleted_notes": deleted_notes,
            "deleted_cards": deleted_cards,
        }

    def add_note(
        self,
        *,
        deck: str,
        notetype: str,
        fields: dict[str, str],
        tags: list[str] | None,
    ) -> int:
        with self._connect_write() as conn:
            deck_id = self._resolve_deck_id(conn, deck)
            notetype_id, field_names, sort_field_idx, is_cloze = self._load_notetype_schema(
                conn, notetype
            )

            ordered_values: list[str] = []
            for field_name in field_names:
                if field_name not in fields:
                    raise LookupError(f"Missing field '{field_name}' for notetype '{notetype}'.")
                ordered_values.append(str(fields[field_name]))

            note_id = self._allocate_row_id(conn, "notes")
            now_sec = int(time.time())
            sort_idx = sort_field_idx if 0 <= sort_field_idx < len(ordered_values) else 0
            sfld = ordered_values[sort_idx] if ordered_values else ""
            flds = "\x1f".join(ordered_values)
            tag_text = self._format_tags(tags or [])

            conn.execute(
                """
                INSERT INTO notes (id, guid, mid, mod, usn, tags, flds, sfld, csum, flags, data)
                VALUES (?, ?, ?, ?, -1, ?, ?, ?, ?, 0, '')
                """,
                (
                    note_id,
                    self._build_guid(note_id),
                    notetype_id,
                    now_sec,
                    tag_text,
                    flds,
                    sfld,
                    self._field_checksum(ordered_values[0] if ordered_values else ""),
                ),
            )

            template_ords = self._template_ords_for_note(
                conn,
                notetype_id,
                ordered_values,
                is_cloze,
            )
            next_due = self._next_new_due(conn)
            for offset, ord_value in enumerate(template_ords):
                conn.execute(
                    """
                    INSERT INTO cards (
                        id, nid, did, ord, mod, usn, type, queue, due, ivl, factor, reps,
                        lapses, left, odue, odid, flags, data
                    )
                    VALUES (?, ?, ?, ?, ?, -1, 0, 0, ?, 0, 0, 0, 0, 0, 0, 0, 0, '{}')
                    """,
                    (
                        self._allocate_row_id(conn, "cards"),
                        note_id,
                        deck_id,
                        ord_value,
                        now_sec,
                        next_due + offset,
                    ),
                )

            return note_id

    def add_notes(self, notes: list[dict[str, JSONValue]]) -> list[int | None]:
        output: list[int | None] = []
        for item in notes:
            deck = str(item.get("deck") or item.get("deckName") or "").strip()
            notetype = str(item.get("notetype") or item.get("modelName") or "").strip()
            raw_fields = item.get("fields")
            raw_tags = item.get("tags")

            if not deck or not notetype or not isinstance(raw_fields, dict):
                output.append(None)
                continue

            try:
                note_id = self.add_note(
                    deck=deck,
                    notetype=notetype,
                    fields={str(k): str(v) for k, v in raw_fields.items()},
                    tags=self._coerce_tags(raw_tags),
                )
            except Exception:
                output.append(None)
            else:
                output.append(note_id)
        return output

    def update_note(
        self,
        *,
        note_id: int,
        fields: dict[str, str] | None,
        tags: list[str] | None,
    ) -> dict[str, JSONValue]:
        with self._connect_write() as conn:
            row = conn.execute(
                "SELECT id, mid, tags, flds FROM notes WHERE id = ?",
                (note_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"Note not found: {note_id}")

            notetype_id = int(row["mid"])
            field_names, sort_idx = self._field_schema_for_mid(conn, notetype_id)
            current_values = self._split_fields(str(row["flds"] or ""))
            if len(current_values) < len(field_names):
                current_values.extend([""] * (len(field_names) - len(current_values)))

            updated_fields = False
            updated_tags = False
            now_sec = int(time.time())

            if fields:
                name_to_ord = {name: idx for idx, name in enumerate(field_names)}
                for key, value in fields.items():
                    if key not in name_to_ord:
                        raise LookupError(
                            f"Field '{key}' does not exist in notetype {notetype_id}."
                        )
                    current_values[name_to_ord[key]] = str(value)

                sfld_idx = sort_idx if 0 <= sort_idx < len(current_values) else 0
                conn.execute(
                    """
                    UPDATE notes
                    SET flds = ?, sfld = ?, csum = ?, mod = ?, usn = -1
                    WHERE id = ?
                    """,
                    (
                        "\x1f".join(current_values),
                        current_values[sfld_idx] if current_values else "",
                        self._field_checksum(current_values[0] if current_values else ""),
                        now_sec,
                        note_id,
                    ),
                )
                updated_fields = True

            if tags is not None:
                conn.execute(
                    "UPDATE notes SET tags = ?, mod = ?, usn = -1 WHERE id = ?",
                    (self._format_tags(tags), now_sec, note_id),
                )
                updated_tags = True

        return {
            "note_id": note_id,
            "updated_fields": updated_fields,
            "updated_tags": updated_tags,
        }

    def delete_notes(self, note_ids: list[int]) -> dict[str, JSONValue]:
        normalized_ids = sorted({int(nid) for nid in note_ids if int(nid) > 0})
        if not normalized_ids:
            return {
                "requested": 0,
                "deleted_notes": 0,
                "deleted_cards": 0,
                "missing_note_ids": [],
            }

        with self._connect_write() as conn:
            placeholders = ", ".join(["?"] * len(normalized_ids))
            existing_rows = conn.execute(
                f"SELECT id FROM notes WHERE id IN ({placeholders})",
                tuple(normalized_ids),
            ).fetchall()
            existing_ids = [int(row["id"]) for row in existing_rows]
            if not existing_ids:
                return {
                    "requested": len(normalized_ids),
                    "deleted_notes": 0,
                    "deleted_cards": 0,
                    "missing_note_ids": normalized_ids,
                }

            existing_set = set(existing_ids)
            missing_ids = [nid for nid in normalized_ids if nid not in existing_set]
            note_placeholders = ", ".join(["?"] * len(existing_ids))

            card_rows = conn.execute(
                f"SELECT id FROM cards WHERE nid IN ({note_placeholders})",
                tuple(existing_ids),
            ).fetchall()
            card_ids = [int(row["id"]) for row in card_rows]

            self._insert_graves(conn, card_ids, grave_type=0)
            self._insert_graves(conn, existing_ids, grave_type=1)

            deleted_cards = int(
                conn.execute(
                    f"DELETE FROM cards WHERE nid IN ({note_placeholders})",
                    tuple(existing_ids),
                ).rowcount
            )
            deleted_notes = int(
                conn.execute(
                    f"DELETE FROM notes WHERE id IN ({note_placeholders})",
                    tuple(existing_ids),
                ).rowcount
            )

        return {
            "requested": len(normalized_ids),
            "deleted_notes": deleted_notes,
            "deleted_cards": deleted_cards,
            "missing_note_ids": missing_ids,
        }

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

    def add_tags(self, note_ids: list[int], tags: list[str]) -> dict[str, JSONValue]:
        normalized_note_ids = sorted({int(nid) for nid in note_ids if int(nid) > 0})
        normalized_tags = self._coerce_tags(tags)
        if not normalized_note_ids or not normalized_tags:
            return {"updated": 0, "note_ids": [], "tags": normalized_tags}

        lower_to_canonical = {tag.lower(): tag for tag in normalized_tags}

        with self._connect_write() as conn:
            placeholders = ", ".join(["?"] * len(normalized_note_ids))
            rows = conn.execute(
                f"SELECT id, tags FROM notes WHERE id IN ({placeholders})",
                tuple(normalized_note_ids),
            ).fetchall()

            now_sec = int(time.time())
            updates: list[tuple[str, int, int]] = []
            for row in rows:
                existing = self._parse_tags(str(row["tags"] or ""))
                merged: dict[str, str] = {tag.lower(): tag for tag in existing}
                merged.update(lower_to_canonical)
                merged_tags = [merged[key] for key in sorted(merged)]
                updates.append((self._format_tags(merged_tags), now_sec, int(row["id"])))

            conn.executemany(
                "UPDATE notes SET tags = ?, mod = ?, usn = -1 WHERE id = ?",
                updates,
            )

        return {
            "updated": len(updates),
            "note_ids": [entry[2] for entry in updates],
            "tags": normalized_tags,
        }

    def remove_tags(self, note_ids: list[int], tags: list[str]) -> dict[str, JSONValue]:
        normalized_note_ids = sorted({int(nid) for nid in note_ids if int(nid) > 0})
        normalized_tags = self._coerce_tags(tags)
        if not normalized_note_ids or not normalized_tags:
            return {"updated": 0, "note_ids": [], "tags": normalized_tags}

        removals = {tag.lower() for tag in normalized_tags}

        with self._connect_write() as conn:
            placeholders = ", ".join(["?"] * len(normalized_note_ids))
            rows = conn.execute(
                f"SELECT id, tags FROM notes WHERE id IN ({placeholders})",
                tuple(normalized_note_ids),
            ).fetchall()

            now_sec = int(time.time())
            updates: list[tuple[str, int, int]] = []
            for row in rows:
                existing = self._parse_tags(str(row["tags"] or ""))
                kept = [tag for tag in existing if tag.lower() not in removals]
                updates.append((self._format_tags(kept), now_sec, int(row["id"])))

            conn.executemany(
                "UPDATE notes SET tags = ?, mod = ?, usn = -1 WHERE id = ?",
                updates,
            )

        return {
            "updated": len(updates),
            "note_ids": [entry[2] for entry in updates],
            "tags": normalized_tags,
        }

    def answer_card(self, card_id: int, ease: int) -> dict[str, JSONValue]:
        if ease not in {1, 2, 3, 4}:
            raise ValueError("ease must be one of 1, 2, 3, 4")

        with self._connect_write() as conn:
            row = conn.execute(
                """
                SELECT
                    id, nid, did, ord, mod, usn, type, queue, due, ivl, factor, reps,
                    lapses, left, odue, odid, flags, data
                FROM cards
                WHERE id = ?
                """,
                (card_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"Card not found: {card_id}")

            col_row = conn.execute("SELECT crt FROM col LIMIT 1").fetchone()
            col_crt_sec = int(col_row["crt"]) if col_row is not None else int(time.time())

            scheduler, desired_retention, learn_count, relearn_count = self._build_scheduler(
                conn, int(row["did"])
            )
            review_dt = datetime.now(UTC)

            fsrs_card = self._card_row_to_fsrs(row, col_crt_sec=col_crt_sec, now_dt=review_dt)
            next_card, _review_log = scheduler.review_card(
                fsrs_card,
                Rating(ease),
                review_datetime=review_dt,
            )

            (
                new_type,
                new_queue,
                new_due,
                new_ivl,
                new_left,
                next_due_epoch,
            ) = self._map_fsrs_result_to_anki(
                current_row=row,
                next_card=next_card,
                col_crt_sec=col_crt_sec,
                learn_step_count=learn_count,
                relearn_step_count=relearn_count,
            )

            now_sec = int(review_dt.timestamp())
            reps = int(row["reps"]) + 1
            lapses = int(row["lapses"]) + (1 if ease == 1 else 0)
            raw_data = self._parse_card_data(str(row["data"] or ""))
            data_obj = dict(raw_data) if isinstance(raw_data, dict) else {}
            data_obj.setdefault("pos", int(row["due"]) if int(row["type"]) == 0 else 0)
            data_obj["lrt"] = now_sec
            data_obj["dr"] = round(desired_retention, 2)
            if next_card.stability is not None:
                data_obj["s"] = round(float(next_card.stability), 4)
            if next_card.difficulty is not None:
                data_obj["d"] = round(float(next_card.difficulty), 3)

            data_json = json.dumps(data_obj, separators=(",", ":"))

            conn.execute(
                """
                UPDATE cards
                SET
                    mod = ?,
                    usn = -1,
                    type = ?,
                    queue = ?,
                    due = ?,
                    ivl = ?,
                    reps = ?,
                    lapses = ?,
                    left = ?,
                    data = ?
                WHERE id = ?
                """,
                (
                    now_sec,
                    new_type,
                    new_queue,
                    new_due,
                    new_ivl,
                    reps,
                    lapses,
                    new_left,
                    data_json,
                    card_id,
                ),
            )

            revlog_id = self._allocate_epoch_ms_id(conn, "revlog")
            old_due = int(row["due"])
            old_type = int(row["type"])
            old_queue = int(row["queue"])
            old_ivl = int(row["ivl"])

            logged_ivl = new_ivl if new_queue == 2 else -max(1, int(next_due_epoch - now_sec))
            if old_queue in (1, 3):
                logged_last_ivl = -max(1, int(old_due - now_sec))
            elif old_queue == 2:
                logged_last_ivl = max(1, old_ivl)
            else:
                logged_last_ivl = old_ivl

            if next_card.difficulty is None:
                logged_factor = int(row["factor"])
            else:
                logged_factor = max(100, min(1100, round(float(next_card.difficulty) * 100)))

            if old_type == 2 and ease == 1:
                review_type = 2
            elif old_type == 2:
                review_type = 1
            else:
                review_type = 0

            conn.execute(
                """
                INSERT INTO revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, type)
                VALUES (?, ?, -1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revlog_id,
                    card_id,
                    ease,
                    logged_ivl,
                    logged_last_ivl,
                    logged_factor,
                    0,
                    review_type,
                ),
            )

        return {
            "card_id": card_id,
            "ease": ease,
            "answered": True,
            "queue": new_queue,
            "type": new_type,
            "due": new_due,
            "interval": new_ivl,
        }

    # ---- SQL query helpers ------------------------------------------------

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

    def _coerce_int_value(self, value: JSONValue) -> int | None:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                return None
        return None

    def _coerce_float_value(self, value: JSONValue) -> float | None:
        if isinstance(value, bool):
            return float(int(value))
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
        return None

    def _ensure_write_safe(self) -> None:
        from anki_cli.backends.detect import _anki_process_running, _sqlite_write_locked

        if _anki_process_running() or _sqlite_write_locked(self.db_path):
            raise RuntimeError(
                "Anki Desktop appears to be running while direct write was requested. "
                "Close Anki Desktop or use --backend ankiconnect."
            )

    def _allocate_row_id(self, conn: sqlite3.Connection, table: str) -> int:
        candidate = int(time.time() * 1000)
        while (
            conn.execute(f"SELECT 1 FROM {table} WHERE id = ? LIMIT 1", (candidate,)).fetchone()
            is not None
        ):
            candidate += 1
        return candidate

    def _allocate_epoch_ms_id(self, conn: sqlite3.Connection, table: str) -> int:
        candidate = int(time.time() * 1000)
        while (
            conn.execute(f"SELECT 1 FROM {table} WHERE id = ? LIMIT 1", (candidate,)).fetchone()
            is not None
        ):
            candidate += 1
        return candidate

    def _field_checksum(self, first_field: str) -> int:
        digest = sha1(first_field.encode("utf-8")).hexdigest()
        return int(digest[:8], 16)

    def _coerce_tags(self, value: JSONValue) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            return [part for part in value.replace(",", " ").split(" ") if part.strip()]
        return []

    def _format_tags(self, tags: list[str]) -> str:
        normalized = sorted({tag.strip() for tag in tags if tag.strip()}, key=str.lower)
        if not normalized:
            return ""
        return f" {' '.join(normalized)} "

    def _build_guid(self, note_id: int) -> str:
        base = f"{note_id:x}"
        return f"ankicli-{base[-10:]}"

    def _insert_graves(self, conn: sqlite3.Connection, oids: list[int], grave_type: int) -> None:
        if not oids:
            return
        conn.executemany(
            "INSERT OR IGNORE INTO graves (oid, type, usn) VALUES (?, ?, -1)",
            [(oid, grave_type) for oid in oids],
        )

    def _resolve_deck_id(self, conn: sqlite3.Connection, deck_name: str) -> int:
        row = conn.execute(
            "SELECT id FROM decks WHERE name = ?",
            (deck_name.strip(),),
        ).fetchone()
        if row is None:
            raise LookupError(f"Deck not found: {deck_name}")
        return int(row["id"])

    def _load_notetype_schema(
        self,
        conn: sqlite3.Connection,
        notetype_name: str,
    ) -> tuple[int, list[str], int, bool]:
        row = conn.execute(
            "SELECT id, config FROM notetypes WHERE name = ?",
            (notetype_name.strip(),),
        ).fetchone()
        if row is None:
            raise LookupError(f"Notetype not found: {notetype_name}")

        mid = int(row["id"])
        config = self._decode_notetype_config(bytes(row["config"] or b""), ntid=mid)
        fields, sort_idx = self._field_schema_for_mid(conn, mid)
        is_cloze = int(config.kind) == 1
        return mid, fields, int(config.sort_field_idx or sort_idx), is_cloze

    def _field_schema_for_mid(
        self, conn: sqlite3.Connection, mid: int
    ) -> tuple[list[str], int]:
        field_rows = conn.execute(
            """
            SELECT ord, name
            FROM fields
            WHERE ntid = ?
            ORDER BY ord
            """,
            (mid,),
        ).fetchall()
        field_names = [str(row["name"]) for row in field_rows]
        nt_row = conn.execute(
            "SELECT config FROM notetypes WHERE id = ?",
            (mid,),
        ).fetchone()
        if nt_row is None:
            return field_names, 0
        config = self._decode_notetype_config(bytes(nt_row["config"] or b""), ntid=mid)
        return field_names, int(config.sort_field_idx)

    def _template_ords_for_note(
        self,
        conn: sqlite3.Connection,
        mid: int,
        field_values: list[str],
        is_cloze: bool,
    ) -> list[int]:
        if is_cloze:
            import re

            text = "\n".join(field_values)
            matches = {int(m.group(1)) for m in re.finditer(r"\{\{c(\d+)::", text)}
            if not matches:
                return [0]
            return sorted({max(0, idx - 1) for idx in matches})

        rows = conn.execute(
            "SELECT ord FROM templates WHERE ntid = ? ORDER BY ord",
            (mid,),
        ).fetchall()
        ords = [int(row["ord"]) for row in rows]
        return ords if ords else [0]

    def _next_new_due(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(due), 0) AS max_due FROM cards WHERE queue = 0"
        ).fetchone()
        return int(row["max_due"] or 0) + 1

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
                    queue = CASE
                        WHEN type = 0 THEN 0
                        WHEN type = 2 THEN 2
                        WHEN type = 3 THEN 3
                        ELSE 1
                    END,
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

    def _build_scheduler(
        self,
        conn: sqlite3.Connection,
        deck_id: int,
    ) -> tuple[Scheduler, float, int, int]:
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

        params = self._pick_fsrs_parameters(cfg)
        desired_retention = (
            deck_retention
            if deck_retention is not None
            else float(cfg.desired_retention or 0.9)
        )
        learning_steps = self._to_timedeltas(
            cfg.learn_steps,
            default=[1.0, 10.0],
            assume_minutes=True,
        )
        relearning_steps = self._to_timedeltas(
            cfg.relearn_steps, default=[10.0], assume_minutes=True
        )
        max_interval = int(cfg.maximum_review_interval or 36500)

        scheduler = Scheduler(
            parameters=params,
            desired_retention=desired_retention,
            learning_steps=learning_steps,
            relearning_steps=relearning_steps,
            maximum_interval=max_interval,
        )
        return scheduler, desired_retention, len(learning_steps), len(relearning_steps)

    def _pick_fsrs_parameters(self, cfg: DeckConfigConfig) -> list[float]:
        for candidate in (cfg.fsrs_params_6, cfg.fsrs_params_5, cfg.fsrs_params_4):
            values = [float(item) for item in candidate]
            if len(values) >= 19:
                return values
        return [
            0.212,
            1.2931,
            2.3065,
            8.2956,
            6.4133,
            0.8334,
            3.0194,
            0.001,
            1.8722,
            0.1666,
            0.796,
            1.4835,
            0.0614,
            0.2629,
            1.6483,
            0.6014,
            1.8729,
            0.5425,
            0.0912,
            0.0658,
            0.1542,
        ]

    def _to_timedeltas(
        self,
        values: list[float],
        *,
        default: list[float],
        assume_minutes: bool,
    ) -> list[timedelta]:
        source = values or default
        out: list[timedelta] = []
        for value in source:
            raw = float(value)
            if raw <= 0:
                continue
            seconds = raw * 60.0 if assume_minutes else raw
            out.append(timedelta(seconds=max(1, round(seconds))))
        return out if out else [timedelta(seconds=60)]

    def _card_row_to_fsrs(
        self,
        row: sqlite3.Row,
        *,
        col_crt_sec: int,
        now_dt: datetime,
    ) -> FSRSCard:
        raw_data = self._parse_card_data(str(row["data"] or ""))
        data: dict[str, JSONValue] = (
            {str(key): cast(JSONValue, value) for key, value in raw_data.items()}
            if isinstance(raw_data, dict)
            else {}
        )

        stability = self._coerce_float_value(data.get("s"))
        difficulty = self._coerce_float_value(data.get("d"))

        last_review: datetime | None = None
        lrt_value = self._coerce_int_value(data.get("lrt"))
        if lrt_value is not None:
            try:
                last_review = datetime.fromtimestamp(lrt_value, tz=UTC)
            except (TypeError, ValueError, OSError):
                last_review = None

        card_type = int(row["type"])
        queue = int(row["queue"])
        due_raw = int(row["due"])
        if card_type == 2:
            crt_day = int(col_crt_sec // 86400)
            due_dt = datetime.fromtimestamp((crt_day + due_raw) * 86400, tz=UTC)
            state = State.Review
        elif queue in (1, 3) or card_type in (1, 3):
            due_dt = datetime.fromtimestamp(due_raw, tz=UTC)
            state = State.Relearning if card_type == 3 or queue == 3 else State.Learning
        else:
            due_dt = now_dt
            state = State.Learning

        left_raw = int(row["left"])
        step = 0 if left_raw > 0 else None

        return FSRSCard(
            card_id=int(row["id"]),
            state=state,
            step=step,
            stability=stability,
            difficulty=difficulty,
            due=due_dt,
            last_review=last_review,
        )

    def _map_fsrs_result_to_anki(
        self,
        *,
        current_row: sqlite3.Row,
        next_card: FSRSCard,
        col_crt_sec: int,
        learn_step_count: int,
        relearn_step_count: int,
    ) -> tuple[int, int, int, int, int, int]:
        now_dt = datetime.now(UTC)
        next_due_dt = next_card.due if next_card.due is not None else now_dt
        next_due_epoch = int(next_due_dt.timestamp())

        if next_card.state == State.Review:
            crt_day = int(col_crt_sec // 86400)
            due_days = max(0, int(next_due_epoch // 86400) - crt_day)
            ivl_days = max(1, round((next_due_dt - now_dt).total_seconds() / 86400.0))
            return (2, 2, due_days, ivl_days, 0, next_due_epoch)

        if next_card.state == State.Relearning:
            total = max(1, relearn_step_count)
            step = int(next_card.step or 0)
            remaining = max(1, total - step)
            left = (remaining * 1000) + remaining
            return (3, 1, next_due_epoch, 0, left, next_due_epoch)

        # Learning (new or ongoing)
        old_type = int(current_row["type"])
        new_type = 1 if old_type != 2 else old_type
        total = max(1, learn_step_count)
        step = int(next_card.step or 0)
        remaining = max(1, total - step)
        left = (remaining * 1000) + remaining
        return (new_type, 1, next_due_epoch, 0, left, next_due_epoch)

    def _decode_message(self, message: Any, blob: bytes, *, context: str) -> Any:
        try:
            return message.parse(blob)
        except Exception as exc:
            raise ValueError(
                f"Failed to decode protobuf for {context} ({len(blob)} bytes)."
            ) from exc
    
    
    def _decode_notetype_config(self, blob: bytes, *, ntid: int) -> NotetypeConfig:
        return self._decode_message(
            NotetypeConfig(),
            blob,
            context=f"notetypes.config ntid={ntid}",
        )
    
    
    def _decode_field_config(self, blob: bytes, *, ntid: int, ord_: int) -> NotetypeFieldConfig:
        return self._decode_message(
            NotetypeFieldConfig(),
            blob,
            context=f"fields.config ntid={ntid} ord={ord_}",
        )
    
    
    def _decode_template_config(
        self, blob: bytes, *, ntid: int, ord_: int
    ) -> NotetypeTemplateConfig:
        return self._decode_message(
            NotetypeTemplateConfig(),
            blob,
            context=f"templates.config ntid={ntid} ord={ord_}",
        )
    
    
    def _decode_deck_common(self, blob: bytes, *, did: int) -> DeckCommon:
        return self._decode_message(
            DeckCommon(),
            blob,
            context=f"decks.common did={did}",
        )
    
    
    def _decode_deck_kind(self, blob: bytes, *, did: int) -> DeckKindContainer:
        return self._decode_message(
            DeckKindContainer(),
            blob,
            context=f"decks.kind did={did}",
        )
    
    
    def _decode_deck_config(self, blob: bytes, *, dcid: int) -> DeckConfigConfig:
        return self._decode_message(
            DeckConfigConfig(),
            blob,
            context=f"deck_config.config id={dcid}",
        )
    
    
    def _load_notetype_parts(
        self,
        conn: sqlite3.Connection,
    ) -> tuple[
        dict[int, list[dict[str, JSONValue]]],
        dict[int, list[dict[str, JSONValue]]],
    ]:
        fields_by_ntid: dict[int, list[dict[str, JSONValue]]] = {}
        templates_by_ntid: dict[int, list[dict[str, JSONValue]]] = {}
    
        field_rows = conn.execute(
            """
            SELECT ntid, ord, name, config
            FROM fields
            ORDER BY ntid, ord
            """
        ).fetchall()
    
        for row in field_rows:
            ntid = int(row["ntid"])
            ord_ = int(row["ord"])
            name = str(row["name"])
            cfg_blob = bytes(row["config"] or b"")
            cfg = self._decode_field_config(cfg_blob, ntid=ntid, ord_=ord_)
    
            fields_by_ntid.setdefault(ntid, []).append(
                {
                    "ord": ord_,
                    "name": name,
                    "font": cfg.font_name,
                    "size": int(cfg.font_size),
                    "rtl": bool(cfg.rtl),
                    "sticky": bool(cfg.sticky),
                    "plain_text": bool(cfg.plain_text),
                }
            )
    
        template_rows = conn.execute(
            """
            SELECT ntid, ord, name, config
            FROM templates
            ORDER BY ntid, ord
            """
        ).fetchall()
    
        for row in template_rows:
            ntid = int(row["ntid"])
            ord_ = int(row["ord"])
            name = str(row["name"])
            cfg_blob = bytes(row["config"] or b"")
            cfg = self._decode_template_config(cfg_blob, ntid=ntid, ord_=ord_)
    
            templates_by_ntid.setdefault(ntid, []).append(
                {
                    "ord": ord_,
                    "name": name,
                    "qfmt": cfg.q_format,
                    "afmt": cfg.a_format,
                    "qfmt_browser": cfg.q_format_browser,
                    "afmt_browser": cfg.a_format_browser,
                }
            )
    
        return fields_by_ntid, templates_by_ntid
    
    
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

    def _decode_due(
        self,
        *,
        card_type: int,
        queue: int,
        due_raw: int,
        col_crt_sec: int | None,
    ) -> dict[str, JSONValue]:
        if card_type == 0:
            return {"kind": "new_position", "raw": due_raw, "position": due_raw}

        if card_type in (1, 3):
            return {"kind": "learn_epoch_secs", "raw": due_raw, "epoch_secs": due_raw}

        if card_type == 2:
            out: dict[str, JSONValue] = {
                "kind": "review_day_index",
                "raw": due_raw,
                "day_index": due_raw,
            }
            if col_crt_sec is not None:
                crt_day = int(col_crt_sec // 86400)
                out["epoch_secs"] = int((crt_day + due_raw) * 86400)
            return out

        return {"kind": "raw", "raw": due_raw, "queue": queue, "type": card_type}


    def _decode_left(self, left_raw: int) -> dict[str, int]:
        if left_raw < 0:
            return {"raw": left_raw}
        return {
            "raw": left_raw,
            "today_remaining": left_raw // 1000,
            "until_graduation": left_raw % 1000,
        }


    def _parse_card_data(self, raw: str) -> JSONValue:
        stripped = raw.strip()
        if not stripped:
            return {}

        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return raw

        if isinstance(parsed, (dict, list, str, int, float, bool)) or parsed is None:
            return parsed
        return raw

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