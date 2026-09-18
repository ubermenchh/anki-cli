"""Notes and tags: search, reads, ``add_note(s)``/``update_note``/``delete_notes``
with reqs-based card generation, and the tag mutators.
"""

from __future__ import annotations

import sqlite3
import time

from anki_cli.db.codec import _strip_html_preserving_media_filenames
from anki_cli.db.deck_lookup import DeckLookupMixin
from anki_cli.db.errors import DuplicateNoteError, EmptyNoteError, NoteRejectedError
from anki_cli.db.notetypes import NotetypesMixin
from anki_cli.db.search_sql import compile_note_query
from anki_cli.models.output import JSONValue


class NotesMixin(NotetypesMixin, DeckLookupMixin):
    """Public note and tag API."""

    def find_notes(self, query: str) -> list[int]:
        compiled = compile_note_query(query, ctx=self._search_context())

        joins_sql = ""
        if compiled.joins:
            joins_sql = "\n            " + "\n            ".join(compiled.joins)

        sql = f"""
            SELECT DISTINCT n.id
            FROM notes AS n{joins_sql}
            WHERE {compiled.where}
            ORDER BY n.id
        """

        with self._connect() as conn:
            rows = conn.execute(sql, compiled.params).fetchall()
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
            # Anki declares ``sfld integer`` so numeric sort fields sort numerically;
            # SQLite's affinity then hands back an int for "42". The API is text.
            "sfld": str(row["sfld"]) if row["sfld"] is not None else "",
            "csum": int(row["csum"]),
            "flags": int(row["flags"]),
            "data": str(row["data"] or ""),
        }

    def get_note_fields(self, note_id: int, fields: list[str] | None = None) -> dict[str, str]:
        with self._connect() as conn:
            row = conn.execute("SELECT mid, flds FROM notes WHERE id = ?", (note_id,)).fetchone()
            if row is None:
                raise LookupError(f"Note not found: {note_id}")

            mid = int(row["mid"])
            names = conn.execute(
                "SELECT ord, name FROM fields WHERE ntid = ? ORDER BY ord",
                (mid,),
            ).fetchall()

        values = self._split_fields(str(row["flds"] or ""))
        out: dict[str, str] = {}
        for item in names:
            ord_ = int(item["ord"])
            name = str(item["name"])
            out[name] = values[ord_] if ord_ < len(values) else ""

        if fields:
            wanted = {f.strip() for f in fields if f.strip()}
            return {k: v for k, v in out.items() if k in wanted}
        return out

    def get_tags(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT tags FROM notes").fetchall()

        tags: set[str] = set()
        for row in rows:
            tags.update(self._parse_tags(str(row["tags"] or "")))
        return sorted(tags, key=str.lower)

    def add_note(
        self,
        deck: str,
        notetype: str,
        fields: dict[str, str],
        tags: list[str] | None = None,
        allow_duplicate: bool = False,
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

            # rslib note_fields_check works on the first field with markup
            # stripped: Empty is checked before Duplicate and is not lifted by
            # allow_duplicate, so "<br>", "&nbsp;" and "<b></b>" are all empty.
            first_stripped = _strip_html_preserving_media_filenames(
                ordered_values[0] if ordered_values else ""
            )
            if not first_stripped.strip():
                raise EmptyNoteError(
                    notetype=notetype, field_name=field_names[0] if field_names else ""
                )
            csum = self._checksum_of_stripped(first_stripped)

            if not allow_duplicate:
                dup_ids = self._find_duplicate_note_ids(
                    conn, notetype_id=notetype_id, csum=csum, first_stripped=first_stripped
                )
                if dup_ids:
                    raise DuplicateNoteError(notetype=notetype, duplicate_ids=dup_ids)

            note_id = self._allocate_row_id(conn, "notes")
            now_sec = int(time.time())
            sfld = self._sort_field_value(ordered_values, sort_field_idx)
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
                    csum,
                ),
            )

            template_ords = self._template_ords_for_note(
                conn,
                notetype_id,
                ordered_values,
                is_cloze,
                field_names=field_names,
                has_tags=bool(tag_text.strip()),
                ensure_not_empty=True,
            )
            self._insert_new_cards(
                conn, note_id=note_id, deck_id=deck_id, ords=template_ords, now_sec=now_sec
            )

            return note_id

    def _insert_new_cards(
        self,
        conn: sqlite3.Connection,
        *,
        note_id: int,
        deck_id: int,
        ords: list[int],
        now_sec: int,
    ) -> None:
        if not ords:
            return
        next_due = self._next_new_due(conn)
        for offset, ord_value in enumerate(ords):
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

    def _generate_missing_cards(
        self,
        conn: sqlite3.Connection,
        *,
        note_id: int,
        notetype_id: int,
        field_values: list[str],
        field_names: list[str],
        has_tags: bool,
        now_sec: int,
    ) -> list[int]:
        """Cards a template now renders for but the note lacks (rslib
        ``generate_cards_for_existing_note``). Existing cards are never removed;
        Anki leaves that to Empty Cards. New cards go to the deck of the note's
        existing cards (home deck if one is on loan)."""
        existing_rows = conn.execute(
            "SELECT ord, did, odid FROM cards WHERE nid = ? ORDER BY ord", (note_id,)
        ).fetchall()
        existing = {int(r["ord"]) for r in existing_rows}
        nt_row = conn.execute(
            "SELECT config FROM notetypes WHERE id = ?", (notetype_id,)
        ).fetchone()
        is_cloze = False
        if nt_row is not None:
            is_cloze = int(
                self._decode_notetype_config(bytes(nt_row["config"] or b""), ntid=notetype_id).kind
            ) == 1
        wanted = self._template_ords_for_note(
            conn,
            notetype_id,
            field_values,
            is_cloze,
            field_names=field_names,
            has_tags=has_tags,
            ensure_not_empty=False,
        )
        missing = sorted(set(wanted) - existing)
        if not missing:
            return []
        if existing_rows:
            first = existing_rows[0]
            deck_id = int(first["odid"]) if int(first["odid"]) != 0 else int(first["did"])
        else:
            deck_id = 1
        self._insert_new_cards(
            conn, note_id=note_id, deck_id=deck_id, ords=missing, now_sec=now_sec
        )
        return missing

    def add_notes(
        self,
        notes: list[dict[str, JSONValue]],
        *,
        allow_duplicate: bool = False,
    ) -> list[int | None]:
        """AnkiConnect ``addNotes`` shape: one id per item, ``None`` for a refused one.

        Only *per-item* problems become ``None`` — a duplicate or empty note
        (``NoteRejectedError``) or a deck/notetype/field that does not exist
        (``LookupError``). Anything else (collection locked, corrupt notetype
        config, ...) would fail every item identically, so it propagates and the
        whole call fails instead of reporting N spurious per-item failures.

        Each item is its own transaction, so without ``allow_duplicate`` the second
        of two identical items in one batch is refused as a duplicate of the first.
        """
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
                    allow_duplicate=allow_duplicate,
                )
            except (NoteRejectedError, LookupError):
                output.append(None)
            else:
                output.append(note_id)
        return output

    def update_note(
        self,
        note_id: int,
        fields: dict[str, str] | None = None,
        tags: list[str] | None = None,
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

                conn.execute(
                    """
                    UPDATE notes
                    SET flds = ?, sfld = ?, csum = ?, mod = ?, usn = -1
                    WHERE id = ?
                    """,
                    (
                        "\x1f".join(current_values),
                        self._sort_field_value(current_values, sort_idx),
                        self._field_checksum(current_values[0] if current_values else ""),
                        now_sec,
                        note_id,
                    ),
                )
                updated_fields = True

            tag_text = str(row["tags"] or "")
            if tags is not None:
                tag_text = self._format_tags(tags)
                conn.execute(
                    "UPDATE notes SET tags = ?, mod = ?, usn = -1 WHERE id = ?",
                    (tag_text, now_sec, note_id),
                )
                updated_tags = True

            generated_cards: list[int] = []
            if updated_fields or updated_tags:
                generated_cards = self._generate_missing_cards(
                    conn,
                    note_id=note_id,
                    notetype_id=notetype_id,
                    field_values=current_values,
                    field_names=field_names,
                    has_tags=bool(tag_text.strip()),
                    now_sec=now_sec,
                )

        return {
            "note_id": note_id,
            "updated_fields": updated_fields,
            "updated_tags": updated_tags,
            "generated_cards": generated_cards,
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

    def get_tag_counts(self) -> list[dict[str, JSONValue]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT tags FROM notes").fetchall()

        counts: dict[str, int] = {}
        for row in rows:
            for tag in self._parse_tags(str(row["tags"] or "")):
                counts[tag] = counts.get(tag, 0) + 1

        return [{"tag": tag, "count": counts[tag]} for tag in sorted(counts, key=str.lower)]

    def rename_tag(self, old_tag: str, new_tag: str) -> dict[str, JSONValue]:
        source = old_tag.strip()
        target = new_tag.strip()
        if not source or not target:
            raise ValueError("Both tags are required.")

        with self._connect_write() as conn:
            rows = conn.execute("SELECT id, tags FROM notes").fetchall()
            updates: list[tuple[str, int, int]] = []
            now_sec = int(time.time())

            for row in rows:
                tags = self._parse_tags(str(row["tags"] or ""))
                if source not in tags:
                    continue
                merged = [target if t == source else t for t in tags]
                merged = sorted(set(merged), key=str.lower)
                updates.append((self._format_tags(merged), now_sec, int(row["id"])))

            if updates:
                conn.executemany(
                    "UPDATE notes SET tags = ?, mod = ?, usn = -1 WHERE id = ?",
                    updates,
                )

        return {"from": source, "to": target, "updated": len(updates)}

    def _find_duplicate_note_ids(
        self,
        conn: sqlite3.Connection,
        *,
        notetype_id: int,
        csum: int,
        first_stripped: str,
    ) -> list[int]:
        """Ids of existing notes Anki would flag as duplicates, ascending.

        Follows rslib's ``is_duplicate``: candidates are looked up by ``csum``
        scoped to the notetype (``csum`` alone collides across notetypes that
        share a front), then each hit is confirmed by comparing its stripped
        first field with ``first_stripped`` — the csum is only 32 bits. The
        caller has already rejected an empty first field.
        """
        rows = conn.execute(
            "SELECT id, flds FROM notes WHERE csum = ? AND mid = ? ORDER BY id",
            (csum, notetype_id),
        ).fetchall()
        matches: list[int] = []
        for row in rows:
            existing_first = self._split_fields(str(row["flds"] or ""))
            existing_stripped = _strip_html_preserving_media_filenames(
                existing_first[0] if existing_first else ""
            )
            if existing_stripped == first_stripped:
                matches.append(int(row["id"]))
        return matches
