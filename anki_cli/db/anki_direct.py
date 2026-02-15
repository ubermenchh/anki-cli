from __future__ import annotations

import shlex
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import betterproto

from anki_cli.backends.protocol import JSONValue
from anki_cli.proto.anki.deck_config import DeckConfigConfig
from anki_cli.proto.anki.decks import DeckCommon, DeckKindContainer
from anki_cli.proto.anki.notetypes import (
    NotetypeConfig,
    NotetypeFieldConfig,
    NotetypeTemplateConfig,
)


class AnkiDirectReadStore:
    """Read-only helpers for Anki's collection.anki21b schema."""

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
                "ord": int(item["ord"]),
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