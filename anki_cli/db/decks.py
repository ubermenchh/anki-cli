"""Decks and deck options: reads, create/rename/delete, ``set_deck_config``.

Sits above ``CardsMixin`` because ``get_deck`` reports due counts. The small
lookups other mixins need live in ``deck_lookup.py``.
"""

from __future__ import annotations

import sqlite3
import time

import betterproto

from anki_cli.db.cards import RESTORED_DUE_SQL, CardsMixin, queue_from_type_sql
from anki_cli.models.output import JSONValue


class DecksMixin(CardsMixin):
    """Public deck API."""

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

    def get_deck(self, name: str) -> dict[str, JSONValue]:
        normalized = name.strip()
        if not normalized:
            raise ValueError("Deck name cannot be empty.")

        deck = next(
            (item for item in self.get_decks() if str(item.get("name", "")) == normalized),
            None,
        )
        if deck is None:
            raise LookupError(f"Deck not found: {normalized}")

        return {
            **deck,
            "due_counts": self.get_due_counts(deck=normalized),
            "next_due": self._get_next_due_for_deck(normalized),
        }

    def get_deck_config(self, name: str) -> dict[str, JSONValue]:
        normalized = name.strip()
        if not normalized:
            raise ValueError("Deck name cannot be empty.")

        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, kind FROM decks WHERE name = ?",
                (normalized,),
            ).fetchone()
            if row is None:
                raise LookupError(f"Deck not found: {normalized}")

            did = int(row["id"])
            deck_kind = self._decode_deck_kind(bytes(row["kind"] or b""), did=did)
            kind_name, kind_msg = betterproto.which_one_of(deck_kind, "kind")
            if kind_name != "normal" or kind_msg is None:
                raise ValueError(f"Deck '{normalized}' is not a normal deck.")

            config_id = int(kind_msg.config_id)
            cfg_row = conn.execute(
                "SELECT id, name, config FROM deck_config WHERE id = ?",
                (config_id,),
            ).fetchone()
            if cfg_row is None:
                raise LookupError(f"Deck config not found: {config_id}")

        cfg = self._decode_deck_config(bytes(cfg_row["config"] or b""), dcid=config_id)
        return {
            "deck": normalized,
            "config_id": config_id,
            "config_name": str(cfg_row["name"]),
            "config": self._deck_config_to_dict(cfg),
        }

    def create_deck(self, name: str) -> dict[str, JSONValue]:
        return self.write_deck(name=name)

    def rename_deck(self, old_name: str, new_name: str) -> dict[str, JSONValue]:
        source = old_name.strip()
        target = new_name.strip()
        if not source or not target:
            raise ValueError("Deck names cannot be empty.")
        if source == target:
            return {
                "from": source,
                "to": target,
                "renamed_decks": 0,
                "unchanged": True,
                "items": [],
            }

        with self._connect_write() as conn:
            found = self._deck_subtree(conn, source)
            if found is None:
                raise LookupError(f"Deck not found: {source}")
            rows = found.rows

            # The target may already exist only if it is the deck being renamed
            # (a case-only rename such as ``école`` -> ``École``).
            scoped_ids = {int(row["id"]) for row in rows}
            taken = self._deck_subtree(conn, target)
            if taken is not None and any(int(row["id"]) not in scoped_ids for row in taken.rows):
                raise ValueError(f"Target deck path already exists: {target}")

            now_sec = int(time.time())
            temp_prefix = f"__anki_cli_tmp_{int(time.time() * 1000)}__"
            plan: list[tuple[int, str, str, str]] = []
            for row in rows:
                did = int(row["id"])
                current_name = str(row["name"])
                # Slice on the stored name: ``source`` may differ in case.
                suffix = current_name[len(found.name) :]
                temp_name = f"{temp_prefix}{suffix}"
                final_name = f"{target}{suffix}"
                plan.append((did, current_name, temp_name, final_name))

            for did, _from_name, temp_name, _to_name in plan:
                conn.execute(
                    "UPDATE decks SET name = ?, mtime_secs = ?, usn = -1 WHERE id = ?",
                    (temp_name, now_sec, did),
                )

            items: list[dict[str, JSONValue]] = []
            for did, from_name, _temp_name, to_name in plan:
                conn.execute(
                    "UPDATE decks SET name = ?, mtime_secs = ?, usn = -1 WHERE id = ?",
                    (to_name, now_sec, did),
                )
                items.append({"id": did, "from": from_name, "to": to_name})

        return {
            "from": source,
            "to": target,
            "renamed_decks": len(items),
            "items": items,
        }

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
            found = self._deck_subtree(conn, normalized)
            deck_rows = found.rows if found is not None else []
            if not deck_rows:
                return {
                    "deck": normalized,
                    "deleted": False,
                    "deleted_decks": 0,
                    "deleted_notes": 0,
                    "deleted_cards": 0,
                    "returned_cards": 0,
                }

            deck_ids = [int(row["id"]) for row in deck_rows]
            if 1 in deck_ids:
                raise ValueError("Cannot delete the Default deck.")

            # Filtered decks only borrow cards; deleting one must send the cards
            # back to their home deck (did = odid, due = odue) rather than delete
            # them. Normal decks own their cards, including any currently on loan
            # to a filtered deck (odid = this deck).
            filtered_ids: list[int] = []
            normal_ids: list[int] = []
            for row in deck_rows:
                did = int(row["id"])
                kind = self._decode_deck_kind(bytes(row["kind"] or b""), did=did)
                kind_name, _ = betterproto.which_one_of(kind, "kind")
                if kind_name == "filtered":
                    filtered_ids.append(did)
                elif kind_name == "normal":
                    normal_ids.append(did)
                else:
                    # A destructive dispatch must fail closed on a malformed blob.
                    raise ValueError(
                        f"Deck {did} ({row['name']}) has an unknown kind; refusing to delete."
                    )

            now_sec = int(time.time())
            returned_cards = 0
            if filtered_ids:
                returned_cards = self._return_cards_from_filtered_decks(
                    conn, filtered_ids, now_sec=now_sec
                )

            deleted_cards = 0
            deleted_notes = 0
            if normal_ids:
                deleted_cards, deleted_notes = self._delete_cards_owned_by_decks(
                    conn, normal_ids
                )

            deck_placeholders = ", ".join(["?"] * len(deck_ids))
            deleted_decks = int(
                conn.execute(
                    f"DELETE FROM decks WHERE id IN ({deck_placeholders})",
                    tuple(deck_ids),
                ).rowcount
            )
            self._insert_graves(conn, deck_ids, grave_type=2)

        return {
            "deck": normalized,
            "deleted": deleted_decks > 0,
            "deleted_decks": deleted_decks,
            "deleted_notes": deleted_notes,
            "deleted_cards": deleted_cards,
            "returned_cards": returned_cards,
        }

    def _delete_cards_owned_by_decks(
        self,
        conn: sqlite3.Connection,
        deck_ids: list[int],
    ) -> tuple[int, int]:
        """Delete every card owned by ``deck_ids`` plus notes left with no cards.

        A card is owned by a deck if it lives there (``did``) or is on loan from
        there to a filtered deck (``odid``). Ids are staged in temp tables so the
        graves come from the same set that was deleted, and so we never build an
        ``IN (?, ?, ...)`` list that could exceed SQLite's variable limit.

        Returns ``(deleted_cards, deleted_notes)``.
        """
        placeholders = ", ".join(["?"] * len(deck_ids))
        # The explicit `odid != 0` is redundant for correctness (deck ids are never
        # 0) but lets SQLite use Anki's partial index `idx_cards_odid ... WHERE
        # odid != 0` via MULTI-INDEX OR instead of scanning the cards table.
        scope = f"(did IN ({placeholders}) OR (odid != 0 AND odid IN ({placeholders})))"
        scope_params = (*deck_ids, *deck_ids)

        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _del_cids (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _del_nids (id INTEGER PRIMARY KEY)")
        conn.execute("DELETE FROM _del_cids")
        conn.execute("DELETE FROM _del_nids")

        conn.execute(
            f"INSERT INTO _del_cids SELECT id FROM cards WHERE {scope}",
            scope_params,
        )
        # Notes whose every card is being deleted are orphaned and go too.
        conn.execute(
            """
            INSERT INTO _del_nids
            SELECT nid
            FROM cards
            WHERE nid IN (SELECT nid FROM cards WHERE id IN (SELECT id FROM _del_cids))
            GROUP BY nid
            HAVING SUM(CASE WHEN id IN (SELECT id FROM _del_cids) THEN 0 ELSE 1 END) = 0
            """
        )

        deleted_notes = int(
            conn.execute("DELETE FROM notes WHERE id IN (SELECT id FROM _del_nids)").rowcount
        )
        deleted_cards = int(
            conn.execute("DELETE FROM cards WHERE id IN (SELECT id FROM _del_cids)").rowcount
        )
        conn.execute(
            "INSERT OR IGNORE INTO graves (oid, type, usn) SELECT id, 0, -1 FROM _del_cids"
        )
        conn.execute(
            "INSERT OR IGNORE INTO graves (oid, type, usn) SELECT id, 1, -1 FROM _del_nids"
        )
        conn.execute("DELETE FROM _del_cids")
        conn.execute("DELETE FROM _del_nids")
        return deleted_cards, deleted_notes

    def _return_cards_from_filtered_decks(
        self,
        conn: sqlite3.Connection,
        filtered_deck_ids: list[int],
        *,
        now_sec: int,
    ) -> int:
        """Move cards out of the given filtered decks back to their home decks.

        Mirrors Anki's ``remove_from_filtered_deck_restoring_queue``: restore
        ``did``/``due`` from ``odid``/``odue`` and recompute ``queue`` from the
        card type. Suspended and buried cards keep their negative queue.
        """
        placeholders = ", ".join(["?"] * len(filtered_deck_ids))
        # SQLite evaluates SET expressions against the pre-update row, so the
        # restored due value has to be spelled out again inside the queue CASE.
        restored_due = RESTORED_DUE_SQL
        cursor = conn.execute(
            f"""
            UPDATE cards
            SET did = odid,
                due = {restored_due},
                odid = 0,
                odue = 0,
                queue = CASE
                    WHEN queue < 0 THEN queue
                    ELSE {queue_from_type_sql(due_expr=restored_due)}
                END,
                mod = ?,
                usn = -1
            WHERE did IN ({placeholders}) AND odid != 0
            """,
            (now_sec, *filtered_deck_ids),
        )
        returned = int(cursor.rowcount)

        # A card in a filtered deck with no home deck recorded is malformed;
        # rather than leave it pointing at a deck that no longer exists, park it
        # in Default (the same recovery Anki's Check Database performs).
        stray = conn.execute(
            f"""
            UPDATE cards
            SET did = 1, mod = ?, usn = -1
            WHERE did IN ({placeholders}) AND odid = 0
            """,
            (now_sec, *filtered_deck_ids),
        )
        return returned + int(stray.rowcount)

    def set_deck_config(
        self,
        name: str,
        updates: dict[str, JSONValue],
    ) -> dict[str, JSONValue]:
        normalized = name.strip()
        if not normalized:
            raise ValueError("Deck name cannot be empty.")
        if not updates:
            return {"deck": normalized, "updated": False, "config": {}}

        with self._connect_write() as conn:
            row = conn.execute(
                "SELECT id, kind FROM decks WHERE name = ?",
                (normalized,),
            ).fetchone()
            if row is None:
                raise LookupError(f"Deck not found: {normalized}")

            did = int(row["id"])
            kind = self._decode_deck_kind(bytes(row["kind"] or b""), did=did)
            kind_name, kind_msg = betterproto.which_one_of(kind, "kind")
            if kind_name != "normal" or kind_msg is None:
                raise ValueError(f"Deck '{normalized}' is not a normal deck.")

            config_id = int(kind_msg.config_id)
            cfg_row = conn.execute(
                "SELECT config FROM deck_config WHERE id = ?",
                (config_id,),
            ).fetchone()
            if cfg_row is None:
                raise LookupError(f"Deck config not found: {config_id}")

            cfg = self._decode_deck_config(bytes(cfg_row["config"] or b""), dcid=config_id)
            applied: dict[str, JSONValue] = {}
            for key, value in updates.items():
                low_key = key.strip().lower()
                if low_key == "new_per_day":
                    parsed = self._coerce_int_value(value)
                    if parsed is None:
                        raise ValueError("new_per_day must be an integer.")
                    cfg.new_per_day = parsed
                    applied["new_per_day"] = parsed
                elif low_key == "reviews_per_day":
                    parsed = self._coerce_int_value(value)
                    if parsed is None:
                        raise ValueError("reviews_per_day must be an integer.")
                    cfg.reviews_per_day = parsed
                    applied["reviews_per_day"] = parsed
                elif low_key == "desired_retention":
                    parsed = self._coerce_float_value(value)
                    if parsed is None:
                        raise ValueError("desired_retention must be a float.")
                    cfg.desired_retention = parsed
                    applied["desired_retention"] = parsed
                elif low_key == "maximum_review_interval":
                    parsed = self._coerce_int_value(value)
                    if parsed is None:
                        raise ValueError("maximum_review_interval must be an integer.")
                    cfg.maximum_review_interval = parsed
                    applied["maximum_review_interval"] = parsed
                elif low_key == "learn_steps":
                    parsed = self._coerce_float_list(value)
                    cfg.learn_steps = parsed
                    applied["learn_steps"] = parsed
                elif low_key == "relearn_steps":
                    parsed = self._coerce_float_list(value)
                    cfg.relearn_steps = parsed
                    applied["relearn_steps"] = parsed
                else:
                    raise ValueError(f"Unsupported deck config key: {key}")

            conn.execute(
                """
                UPDATE deck_config
                SET mtime_secs = ?, usn = -1, config = ?
                WHERE id = ?
                """,
                (int(time.time()), bytes(cfg), config_id),
            )

        return {
            "deck": normalized,
            "updated": bool(applied),
            "config_id": config_id,
            "applied": applied,
            "config": self._deck_config_to_dict(cfg),
        }
