"""Notetypes: reads, ``create_notetype``, field/template/CSS edits, and the
``flds`` rewrites plus ``col.scm`` bumps those edits require.
"""

from __future__ import annotations

import sqlite3
import time

from anki_cli.core.template import (
    _CLOZE_RE,
    SPECIAL_FIELDS,
    TemplateParseError,
    field_is_empty,
    parse_template,
    remove_field_from_template,
    template_renders_with_fields,
    template_requirements,
)
from anki_cli.db.connection import ConnectionMixin
from anki_cli.models.output import JSONValue
from anki_cli.proto.anki.notetypes import (
    NotetypeConfig,
    NotetypeConfigCardRequirement,
    NotetypeConfigCardRequirementKind,
    NotetypeConfigKind,
    NotetypeFieldConfig,
    NotetypeTemplateConfig,
)


class NotetypesMixin(ConnectionMixin):
    """Public notetype API plus the schema helpers notes need."""

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

    def create_notetype(
        self,
        name: str,
        fields: list[str],
        templates: list[dict[str, str]],
        *,
        css: str = "",
        kind: str = "normal",
    ) -> dict[str, JSONValue]:
        normalized = name.strip()
        if not normalized:
            raise ValueError("Notetype name cannot be empty.")
        field_names = [field.strip() for field in fields if field.strip()]
        if not field_names:
            raise ValueError("At least one field is required.")

        cleaned_templates: list[dict[str, str]] = []
        for item in templates:
            tname = str(item.get("name", "")).strip()
            if not tname:
                raise ValueError("Template name cannot be empty.")
            cleaned_templates.append(
                {
                    "name": tname,
                    "front": str(item.get("front", "")),
                    "back": str(item.get("back", "")),
                }
            )
        if not cleaned_templates:
            raise ValueError("At least one template is required.")

        normalized_kind = kind.strip().lower()
        if normalized_kind not in {"normal", "cloze"}:
            raise ValueError("kind must be 'normal' or 'cloze'.")

        with self._connect_write() as conn:
            existing = conn.execute(
                "SELECT id FROM notetypes WHERE name = ?",
                (normalized,),
            ).fetchone()
            if existing is not None:
                raise ValueError(f"Notetype already exists: {normalized}")

            ntid = self._allocate_row_id(conn, "notetypes")
            now_sec = int(time.time())
            config = NotetypeConfig(
                kind=(
                    NotetypeConfigKind.KIND_CLOZE
                    if normalized_kind == "cloze"
                    else NotetypeConfigKind.KIND_NORMAL
                ),
                sort_field_idx=0,
                css=css,
            )
            conn.execute(
                """
                INSERT INTO notetypes (id, name, mtime_secs, usn, config)
                VALUES (?, ?, ?, -1, ?)
                """,
                (ntid, normalized, now_sec, bytes(config)),
            )

            for ord_, field_name in enumerate(field_names):
                conn.execute(
                    """
                    INSERT INTO fields (ntid, ord, name, config)
                    VALUES (?, ?, ?, ?)
                    """,
                    (ntid, ord_, field_name, bytes(NotetypeFieldConfig())),
                )

            for ord_, template in enumerate(cleaned_templates):
                conn.execute(
                    """
                    INSERT INTO templates (ntid, ord, name, mtime_secs, usn, config)
                    VALUES (?, ?, ?, ?, -1, ?)
                    """,
                    (
                        ntid,
                        ord_,
                        template["name"],
                        now_sec,
                        bytes(
                            NotetypeTemplateConfig(
                                q_format=template["front"],
                                a_format=template["back"],
                            )
                        ),
                    ),
                )
            self._recompute_reqs(conn, ntid)

        return {
            "id": ntid,
            "name": normalized,
            "kind": normalized_kind,
            "field_count": len(field_names),
            "template_count": len(cleaned_templates),
            "created": True,
        }

    def add_notetype_field(self, name: str, field_name: str) -> dict[str, JSONValue]:
        normalized_name = name.strip()
        normalized_field = field_name.strip()
        if not normalized_name or not normalized_field:
            raise ValueError("Notetype and field name are required.")

        with self._connect_write() as conn:
            row = conn.execute(
                "SELECT id FROM notetypes WHERE name = ?",
                (normalized_name,),
            ).fetchone()
            if row is None:
                raise LookupError(f"Notetype not found: {normalized_name}")
            ntid = int(row["id"])

            existing = conn.execute(
                "SELECT 1 FROM fields WHERE ntid = ? AND name = ?",
                (ntid, normalized_field),
            ).fetchone()
            if existing is not None:
                return {"name": normalized_name, "field": normalized_field, "added": False}

            max_ord_row = conn.execute(
                "SELECT COALESCE(MAX(ord), -1) AS max_ord FROM fields WHERE ntid = ?",
                (ntid,),
            ).fetchone()
            next_ord = int(max_ord_row["max_ord"]) + 1
            field_count_before = int(
                conn.execute(
                    "SELECT COUNT(*) FROM fields WHERE ntid = ?", (ntid,)
                ).fetchone()[0]
            )
            now_sec = int(time.time())
            conn.execute(
                """
                INSERT INTO fields (ntid, ord, name, config)
                VALUES (?, ?, ?, ?)
                """,
                (ntid, next_ord, normalized_field, bytes(NotetypeFieldConfig())),
            )
            # notes.flds is positional: every existing note needs an empty slot
            # appended or Anki reports a field-count mismatch.
            updated_notes = self._append_field_to_notes(
                conn, ntid=ntid, field_count=field_count_before, now_sec=now_sec
            )
            conn.execute(
                "UPDATE notetypes SET mtime_secs = ?, usn = -1 WHERE id = ?",
                (now_sec, ntid),
            )
            self._recompute_reqs(conn, ntid)
            self._mark_schema_modified(conn)

        return {
            "name": normalized_name,
            "field": normalized_field,
            "added": True,
            "updated_notes": updated_notes,
            "full_sync_required": True,
        }

    def remove_notetype_field(self, name: str, field_name: str) -> dict[str, JSONValue]:
        normalized_name = name.strip()
        normalized_field = field_name.strip()
        if not normalized_name or not normalized_field:
            raise ValueError("Notetype and field name are required.")

        with self._connect_write() as conn:
            row = conn.execute(
                "SELECT id, config FROM notetypes WHERE name = ?",
                (normalized_name,),
            ).fetchone()
            if row is None:
                raise LookupError(f"Notetype not found: {normalized_name}")

            ntid = int(row["id"])
            fields = conn.execute(
                "SELECT ord, name FROM fields WHERE ntid = ? ORDER BY ord",
                (ntid,),
            ).fetchall()
            if len(fields) <= 1:
                raise ValueError("Cannot remove the last remaining field.")

            # Anki declares fields.name COLLATE unicase, so match case-insensitively
            # (add_notetype_field's `name = ?` lookup already does via SQL).
            wanted = normalized_field.casefold()
            target_row = next(
                (item for item in fields if str(item["name"]).casefold() == wanted),
                None,
            )
            if target_row is None:
                raise LookupError(f"Field not found: {normalized_field}")
            removed_ord = int(target_row["ord"])
            stored_field_name = str(target_row["name"])
            old_field_count = len(fields)
            new_field_count = old_field_count - 1
            now_sec = int(time.time())

            conn.execute(
                "DELETE FROM fields WHERE ntid = ? AND ord = ?",
                (ntid, removed_ord),
            )
            conn.execute(
                "UPDATE fields SET ord = ord - 1 WHERE ntid = ? AND ord > ?",
                (ntid, removed_ord),
            )

            # Keep the notetype config consistent with the new field layout.
            config = self._decode_notetype_config(bytes(row["config"] or b""), ntid=ntid)
            sort_idx = int(config.sort_field_idx)
            if sort_idx > removed_ord:
                sort_idx -= 1
            # If the sort field itself was removed, Anki (reposition_sort_idx) keeps
            # the ordinal, so the field that slides into that slot becomes the sort
            # field; the clamp only matters when the removed field was the last one.
            sort_idx = max(0, min(sort_idx, new_field_count - 1))
            config.sort_field_idx = sort_idx

            conn.execute(
                "UPDATE notetypes SET mtime_secs = ?, usn = -1, config = ? WHERE id = ?",
                (now_sec, bytes(config), ntid),
            )
            # Anki drops references to the removed field from every template
            # (falling back to the first remaining field if a front would be
            # left empty), then recomputes the legacy reqs cache from the result.
            remaining_names = [str(f["name"]) for f in fields if int(f["ord"]) != removed_ord]
            self._remove_field_from_templates(
                conn,
                ntid=ntid,
                removed_name=stored_field_name,
                first_remaining_field=remaining_names[0],
                is_cloze=int(config.kind) == 1,
                now_sec=now_sec,
            )
            self._recompute_reqs(conn, ntid)
            self._mark_schema_modified(conn)

            # Field values are stored positionally in notes.flds, so every note of
            # this notetype must drop the removed slot or all later fields shift.
            updated_notes = self._remove_field_from_notes(
                conn,
                ntid=ntid,
                removed_ord=removed_ord,
                field_count=old_field_count,
                sort_idx=sort_idx,
                now_sec=now_sec,
            )

        return {
            "name": normalized_name,
            "field": stored_field_name,
            "removed": True,
            "updated_notes": updated_notes,
            "full_sync_required": True,
        }

    def _append_field_to_notes(
        self,
        conn: sqlite3.Connection,
        *,
        ntid: int,
        field_count: int,
        now_sec: int,
    ) -> int:
        """Append one empty slot to every note's positional field list.

        ``field_count`` is the number of fields *before* the addition; short
        (legacy) rows are padded to it and over-long rows truncated to it first
        so the new slot lands at the right ordinal. Mirrors Anki's
        ``Note::reorder_fields`` for the append case.
        """
        note_rows = conn.execute(
            "SELECT id, flds FROM notes WHERE mid = ?",
            (ntid,),
        ).fetchall()
        if not note_rows:
            return 0

        updates: list[tuple[str, int, int]] = []
        for note_row in note_rows:
            values = self._split_fields(str(note_row["flds"] or ""))[:field_count]
            if len(values) < field_count:
                values.extend([""] * (field_count - len(values)))
            values.append("")
            updates.append(("\x1f".join(values), now_sec, int(note_row["id"])))

        conn.executemany(
            "UPDATE notes SET flds = ?, mod = ?, usn = -1 WHERE id = ?",
            updates,
        )
        return len(updates)

    def _remove_field_from_notes(
        self,
        conn: sqlite3.Connection,
        *,
        ntid: int,
        removed_ord: int,
        field_count: int,
        sort_idx: int,
        now_sec: int,
    ) -> int:
        """Drop ``removed_ord`` from every note's positional field list.

        ``field_count`` is the number of fields *before* removal; ``sort_idx`` is
        the sort field index *after* removal.
        """
        note_rows = conn.execute(
            "SELECT id, flds FROM notes WHERE mid = ?",
            (ntid,),
        ).fetchall()
        if not note_rows:
            return 0

        updates: list[tuple[str, str, int, int, int]] = []
        for note_row in note_rows:
            values = self._split_fields(str(note_row["flds"] or ""))
            if len(values) < field_count:
                values.extend([""] * (field_count - len(values)))
            del values[removed_ord]
            updates.append(
                (
                    "\x1f".join(values),
                    self._sort_field_value(values, sort_idx),
                    self._field_checksum(values[0] if values else ""),
                    now_sec,
                    int(note_row["id"]),
                )
            )

        conn.executemany(
            """
            UPDATE notes
            SET flds = ?, sfld = ?, csum = ?, mod = ?, usn = -1
            WHERE id = ?
            """,
            updates,
        )
        return len(updates)

    def add_notetype_template(
        self,
        name: str,
        template_name: str,
        front: str,
        back: str,
    ) -> dict[str, JSONValue]:
        normalized_name = name.strip()
        normalized_template = template_name.strip()
        if not normalized_name or not normalized_template:
            raise ValueError("Notetype and template name are required.")

        with self._connect_write() as conn:
            row = conn.execute(
                "SELECT id FROM notetypes WHERE name = ?",
                (normalized_name,),
            ).fetchone()
            if row is None:
                raise LookupError(f"Notetype not found: {normalized_name}")
            ntid = int(row["id"])

            existing = conn.execute(
                "SELECT 1 FROM templates WHERE ntid = ? AND name = ?",
                (ntid, normalized_template),
            ).fetchone()
            if existing is not None:
                return {"name": normalized_name, "template": normalized_template, "added": False}

            max_ord_row = conn.execute(
                "SELECT COALESCE(MAX(ord), -1) AS max_ord FROM templates WHERE ntid = ?",
                (ntid,),
            ).fetchone()
            next_ord = int(max_ord_row["max_ord"]) + 1
            now_sec = int(time.time())
            conn.execute(
                """
                INSERT INTO templates (ntid, ord, name, mtime_secs, usn, config)
                VALUES (?, ?, ?, ?, -1, ?)
                """,
                (
                    ntid,
                    next_ord,
                    normalized_template,
                    now_sec,
                    bytes(NotetypeTemplateConfig(q_format=front, a_format=back)),
                ),
            )
            conn.execute(
                "UPDATE notetypes SET mtime_secs = ?, usn = -1 WHERE id = ?",
                (now_sec, ntid),
            )
            self._recompute_reqs(conn, ntid)
            self._mark_schema_modified(conn)

        return {
            "name": normalized_name,
            "template": normalized_template,
            "added": True,
            "full_sync_required": True,
        }

    def edit_notetype_template(
        self,
        name: str,
        template_name: str,
        *,
        front: str | None = None,
        back: str | None = None,
    ) -> dict[str, JSONValue]:
        normalized_name = name.strip()
        normalized_template = template_name.strip()
        if not normalized_name or not normalized_template:
            raise ValueError("Notetype and template name are required.")
        if front is None and back is None:
            raise ValueError("Provide at least one of front/back.")

        with self._connect_write() as conn:
            row = conn.execute(
                """
                SELECT t.ntid AS ntid, t.ord AS ord, t.config AS config
                FROM templates AS t
                JOIN notetypes AS n ON n.id = t.ntid
                WHERE n.name = ? AND t.name = ?
                """,
                (normalized_name, normalized_template),
            ).fetchone()
            if row is None:
                raise LookupError(f"Template not found: {normalized_template}")

            ntid = int(row["ntid"])
            ord_ = int(row["ord"])
            cfg = self._decode_template_config(bytes(row["config"] or b""), ntid=ntid, ord_=ord_)
            if front is not None:
                cfg.q_format = front
            if back is not None:
                cfg.a_format = back

            now_sec = int(time.time())
            conn.execute(
                """
                UPDATE templates
                SET mtime_secs = ?, usn = -1, config = ?
                WHERE ntid = ? AND ord = ?
                """,
                (now_sec, bytes(cfg), ntid, ord_),
            )
            # Sync ships notetypes as whole objects keyed off notetypes.usn.
            conn.execute(
                "UPDATE notetypes SET mtime_secs = ?, usn = -1 WHERE id = ?",
                (now_sec, ntid),
            )
            if front is not None:
                self._recompute_reqs(conn, ntid)

        return {"name": normalized_name, "template": normalized_template, "updated": True}

    def set_notetype_css(self, name: str, css: str) -> dict[str, JSONValue]:
        normalized = name.strip()
        if not normalized:
            raise ValueError("Notetype name cannot be empty.")

        with self._connect_write() as conn:
            row = conn.execute(
                "SELECT id, config FROM notetypes WHERE name = ?",
                (normalized,),
            ).fetchone()
            if row is None:
                raise LookupError(f"Notetype not found: {normalized}")

            ntid = int(row["id"])
            cfg = self._decode_notetype_config(bytes(row["config"] or b""), ntid=ntid)
            cfg.css = css
            conn.execute(
                "UPDATE notetypes SET mtime_secs = ?, usn = -1, config = ? WHERE id = ?",
                (int(time.time()), bytes(cfg), ntid),
            )

        return {"name": normalized, "updated": True, "css": css}

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
        *,
        field_names: list[str] | None = None,
        has_tags: bool = False,
        ensure_not_empty: bool = True,
    ) -> list[int]:
        """Which cards a note should have (rslib ``CardGenContext::new_cards_required``).

        Normal notetypes: a template yields a card only if its front renders
        non-empty given the note's non-empty fields (plus Anki's special fields).
        A template that fails to parse never renders, as in Anki. With
        ``ensure_not_empty`` (new notes) the first template is used when nothing
        would render. The legacy ``reqs`` cache is not consulted, matching modern
        Anki.
        """
        if is_cloze:
            text = "\n".join(field_values)
            # rslib parses the cloze number as u16; anything larger is not a cloze.
            matches = {
                int(m.group(1)) for m in _CLOZE_RE.finditer(text) if int(m.group(1)) <= 0xFFFF
            }
            if not matches:
                return [0] if ensure_not_empty else []
            return sorted({min(499, max(0, idx - 1)) for idx in matches})

        rows = conn.execute(
            "SELECT ord, config FROM templates WHERE ntid = ? ORDER BY ord",
            (mid,),
        ).fetchall()
        if not rows:
            return [0] if ensure_not_empty else []

        names = field_names if field_names is not None else self._field_schema_for_mid(conn, mid)[0]
        nonempty = {
            name
            for name, value in zip(names, field_values, strict=False)
            if not field_is_empty(value)
        }
        for special in SPECIAL_FIELDS:
            if special in names or special == "FrontSide":
                continue
            if special == "Tags" and not has_tags:
                continue
            nonempty.add(special)

        ords: list[int] = []
        for row in rows:
            ord_ = int(row["ord"])
            cfg = self._decode_template_config(bytes(row["config"] or b""), ntid=mid, ord_=ord_)
            try:
                nodes = parse_template(cfg.q_format)
            except TemplateParseError:
                continue
            if template_renders_with_fields(nodes, nonempty):
                ords.append(ord_)
        if not ords and ensure_not_empty:
            return [int(rows[0]["ord"])]
        return ords

    def _remove_field_from_templates(
        self,
        conn: sqlite3.Connection,
        *,
        ntid: int,
        removed_name: str,
        first_remaining_field: str,
        is_cloze: bool,
        now_sec: int,
    ) -> int:
        """rslib ``update_templates_for_renamed_and_removed_fields`` (removal case)."""
        rows = conn.execute(
            "SELECT ord, config FROM templates WHERE ntid = ? ORDER BY ord", (ntid,)
        ).fetchall()
        changed = 0
        for row in rows:
            ord_ = int(row["ord"])
            tcfg = self._decode_template_config(bytes(row["config"] or b""), ntid=ntid, ord_=ord_)
            before = bytes(tcfg)
            for attr, question_side in (
                ("q_format", True),
                ("a_format", False),
                ("q_format_browser", True),
                ("a_format_browser", False),
            ):
                text = getattr(tcfg, attr, "") or ""
                if not text:
                    continue
                setattr(
                    tcfg,
                    attr,
                    remove_field_from_template(
                        text,
                        {removed_name},
                        first_remaining_field=first_remaining_field,
                        is_cloze=is_cloze,
                        question_side=question_side,
                    ),
                )
            after = bytes(tcfg)
            if after != before:
                conn.execute(
                    "UPDATE templates SET config = ?, mtime_secs = ?, usn = -1 "
                    "WHERE ntid = ? AND ord = ?",
                    (after, now_sec, ntid, ord_),
                )
                changed += 1
        return changed

    def _recompute_reqs(self, conn: sqlite3.Connection, ntid: int) -> None:
        """Rewrite the legacy ``reqs`` cache from the templates (rslib
        ``Notetype::updated_requirements``). Modern Anki recomputes this on every
        notetype save; older clients read it to decide which cards to make."""
        nt_row = conn.execute("SELECT config FROM notetypes WHERE id = ?", (ntid,)).fetchone()
        if nt_row is None:
            return
        config = self._decode_notetype_config(bytes(nt_row["config"] or b""), ntid=ntid)
        field_names, _ = self._field_schema_for_mid(conn, ntid)
        rows = conn.execute(
            "SELECT ord, config FROM templates WHERE ntid = ? ORDER BY ord", (ntid,)
        ).fetchall()

        kinds = {
            "any": NotetypeConfigCardRequirementKind.KIND_ANY,
            "all": NotetypeConfigCardRequirementKind.KIND_ALL,
            "none": NotetypeConfigCardRequirementKind.KIND_NONE,
        }
        reqs: list[NotetypeConfigCardRequirement] = []
        for row in rows:
            ord_ = int(row["ord"])
            tcfg = self._decode_template_config(bytes(row["config"] or b""), ntid=ntid, ord_=ord_)
            try:
                kind, ords = template_requirements(parse_template(tcfg.q_format), field_names)
            except TemplateParseError:
                kind, ords = "none", []
            reqs.append(
                NotetypeConfigCardRequirement(card_ord=ord_, kind=kinds[kind], field_ords=ords)
            )
        config.reqs = reqs
        conn.execute("UPDATE notetypes SET config = ? WHERE id = ?", (bytes(config), ntid))

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
