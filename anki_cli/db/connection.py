"""Opening the collection, the write transaction, id allocation, scheduler
timing and the ``col`` bookkeeping every mutator relies on.

``ConnectionMixin`` owns ``__init__`` and ``db_path``; every other mixin
extends it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from anki_cli.db import lock
from anki_cli.db.codec import CodecMixin
from anki_cli.db.deck_names import DECK_SEP_HUMAN, deck_human_to_native
from anki_cli.db.errors import (
    MIN_SUPPORTED_SCHEMA_VERSION,
    DirectWriteBlockedError,
    UnsupportedCollectionError,
)
from anki_cli.db.search_sql import SearchContext
from anki_cli.db.timing import (
    DEFAULT_ROLLOVER_HOUR,
    SchedTiming,
    local_minutes_west_for_stamp,
    sched_timing_today,
)
from anki_cli.models.output import JSONValue

logger = logging.getLogger(__name__)


# ``WHERE id IN (...)`` batch size; stays under SQLite's bound-variable limit
# (999 on older builds).
SQL_IN_CHUNK = 500


class ConnectionMixin(CodecMixin):
    """Helpers for Anki's collection(.anki21b/.anki2) schema."""

    db_path: Path

    def __init__(self, db_path: Path) -> None:
        resolved = db_path.expanduser().resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"Direct collection not found: {resolved}")
        self.db_path = resolved
        self._deck_names_checked = False
        self.deck_name_conflicts: list[str] = []
        """Legacy ``::`` deck names the migration could not rewrite (#75)."""
        self._check_schema_version()

    def _check_schema_version(self) -> None:
        """Refuse legacy collections up front instead of failing mid-command.

        Only the schema version is inspected; a file without a readable
        ``col.ver`` (a non-Anki SQLite file, or a stripped-down test fixture)
        is left for later queries to reject on their own terms.
        """
        # Plain path + query_only rather than a file: URI: SQLite's URI parser
        # treats '#' and '?' in the path as delimiters, so a profile named
        # "Deck#1" would silently open (and create) a different file. The file
        # is known to exist, so the default rwc open cannot create anything.
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=1.0)
        except sqlite3.Error:
            return
        try:
            conn.execute("PRAGMA query_only = ON")
            row = conn.execute("SELECT ver FROM col LIMIT 1").fetchone()
        except sqlite3.Error:
            return
        finally:
            conn.close()
        if row is None or row[0] is None:
            return
        try:
            ver = int(row[0])
        except (TypeError, ValueError):
            return
        if ver < MIN_SUPPORTED_SCHEMA_VERSION:
            raise UnsupportedCollectionError(
                f"Unsupported collection schema {ver} at {self.db_path} "
                f"(need >= {MIN_SUPPORTED_SCHEMA_VERSION}). Open the profile once in "
                "Anki 2.1.50 or newer to upgrade it, then retry."
            )

    @staticmethod
    def _unicase_collation(left: str | None, right: str | None) -> int:
        lval = (left or "").casefold()
        rval = (right or "").casefold()
        return (lval > rval) - (lval < rval)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self._ensure_deck_names_native()
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
        self._ensure_deck_names_native()
        self._ensure_write_safe()
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.create_collation("unicase", self._unicase_collation)
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            conn.execute("BEGIN IMMEDIATE")
            changes_before = conn.total_changes
            yield conn
            if conn.total_changes != changes_before:
                # Anki's sync handshake compares col.mod with the server; rows
                # marked usn = -1 are only scanned if that timestamp moved, so a
                # write that leaves col.mod alone is invisible to the next sync.
                self._touch_collection_modified(conn)
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _ensure_deck_names_native(self) -> None:
        """Once per store, before its first connection: rewrite deck names an older
        anki-cli stored with a literal ``::`` to Anki's ``\\x1f`` (#75). Lookups only
        understand ``\\x1f``, so this has to happen before a read as well as a write.

        Best effort: if Anki looks to be running (the write guard refuses) or the
        rewrite fails, the collection is left as it was and a warning is logged;
        the next command retries. Nothing is written when no legacy name exists.
        """
        if self._deck_names_checked:
            return
        self._deck_names_checked = True
        try:
            probe = sqlite3.connect(str(self.db_path), timeout=1.0)
            try:
                probe.execute("PRAGMA query_only = ON")
                legacy = probe.execute(
                    "SELECT 1 FROM decks WHERE name LIKE '%::%' LIMIT 1"
                ).fetchone()
            finally:
                probe.close()
        except sqlite3.Error:
            return  # not an Anki collection (or no decks table): later queries say so
        if legacy is None:
            return
        try:
            self._ensure_write_safe()
        except DirectWriteBlockedError:
            logger.warning(
                "Deck names with a literal '::' were found in %s but the collection "
                "cannot be written right now (Anki running?); deck lookups by '::' name "
                "will miss them until a command runs with Anki closed.",
                self.db_path,
            )
            return
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.create_collation("unicase", self._unicase_collation)
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            conn.execute("BEGIN IMMEDIATE")
            changes_before = conn.total_changes
            skipped = self._normalize_legacy_deck_names(conn)
            if conn.total_changes != changes_before:
                self._touch_collection_modified(conn)
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            logger.warning("Could not normalise legacy '::' deck names: %s", exc)
            return
        finally:
            conn.close()
        self.deck_name_conflicts = skipped
        for name in skipped:
            logger.warning("Deck %r was left with a literal '::' in its stored name.", name)

    @staticmethod
    def _normalize_legacy_deck_names(conn: sqlite3.Connection) -> list[str]:
        """Rewrite stored names containing ``::`` to ``\\x1f``, bumping mtime and
        usn so the change syncs. Returns the names it had to leave alone: an empty
        component (``A::::B``, ``A::``, ``::A``) is not a valid Anki name, and a
        name whose native spelling is already another deck would break the unique
        name index. Anki's Check Database repairs those; this does not guess."""
        rows = conn.execute("SELECT id, name FROM decks WHERE name LIKE '%::%'").fetchall()
        now_sec = int(time.time())
        skipped: list[str] = []
        for row in rows:
            name = str(row["name"])
            native = deck_human_to_native(name)
            if (
                "" in name.split(DECK_SEP_HUMAN)
                or conn.execute(
                    "SELECT 1 FROM decks WHERE name = ? AND id != ?", (native, row["id"])
                ).fetchone()
            ):
                skipped.append(name)
                continue
            conn.execute(
                "UPDATE decks SET name = ?, mtime_secs = ?, usn = -1 WHERE id = ?",
                (native, now_sec, row["id"]),
            )
        return skipped

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def _touch_collection_modified(self, conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE col SET mod = ?", (self._now_ms(),))

    def _mark_schema_modified(self, conn: sqlite3.Connection) -> None:
        """Record a schema change (Anki: ``set_schema_modified``).

        A differing ``col.scm`` forces a one-way full sync on the next sync, which
        Anki requires whenever fields or templates are added, removed or
        reordered, or the sort field changes.
        """
        now_ms = self._now_ms()
        conn.execute("UPDATE col SET scm = ?, mod = ?", (now_ms, now_ms))

    def _ensure_write_safe(self) -> None:
        # Module attribute lookups on purpose: tests stub the probes on
        # ``anki_cli.db.lock`` and the guard must see the stubs.
        running = lock.anki_process_running()
        try:
            locked = lock.sqlite_write_locked(self.db_path)
        except sqlite3.Error as exc:
            # Fail closed, but as the typed refusal: an inconclusive probe is
            # "blocked", not a raw sqlite3 traceback out of the write path.
            raise DirectWriteBlockedError(
                f"Cannot verify the collection lock state at {self.db_path}: {exc}"
            ) from exc
        if running or locked:
            raise DirectWriteBlockedError(
                "Anki Desktop appears to be running while direct write was requested. "
                "Close Anki Desktop or use --backend ankiconnect."
            )

    def _allocate_row_id(self, conn: sqlite3.Connection, table: str) -> int:
        """Anki ids are epoch milliseconds, nudged forward past any collision."""
        candidate = int(time.time() * 1000)
        while (
            conn.execute(f"SELECT 1 FROM {table} WHERE id = ? LIMIT 1", (candidate,)).fetchone()
            is not None
        ):
            candidate += 1
        return candidate

    def _read_config_json(self, conn: sqlite3.Connection, key: str) -> JSONValue:
        """Value of a key in Anki's ``config`` table (JSON blob), or None if absent."""
        try:
            row = conn.execute("SELECT val FROM config WHERE key = ?", (key,)).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            return None  # stripped-down fixture without a config table
        if row is None:
            return None
        raw = row["val"]
        text = raw.decode("utf-8") if isinstance(raw, bytes | bytearray) else str(raw)
        try:
            return cast(JSONValue, json.loads(text))
        except json.JSONDecodeError:
            return None

    def _timing(self, conn: sqlite3.Connection, now_sec: int) -> SchedTiming:
        """Anki's "today" for this collection (rslib ``timing_for_timestamp``).

        Reads ``crt`` plus the ``schedVer`` / ``rollover`` / ``creationOffset``
        config keys and dispatches exactly like rslib: no ``schedVer`` -> v1
        (plain 86400-second days), no ``creationOffset`` -> v2 legacy cutoff,
        both -> v2 new timezone handling. The current UTC offset is the local
        machine's, as Anki desktop uses.
        """
        row = conn.execute("SELECT crt FROM col LIMIT 1").fetchone()
        crt = int(row["crt"]) if row is not None else now_sec

        sched_ver = self._coerce_int_value(self._read_config_json(conn, "schedVer"))
        rollover: int | None = None
        if sched_ver is not None and sched_ver >= 2:
            configured = self._coerce_int_value(self._read_config_json(conn, "rollover"))
            rollover = configured if configured is not None else DEFAULT_ROLLOVER_HOUR
        creation_west = self._coerce_int_value(self._read_config_json(conn, "creationOffset"))

        return sched_timing_today(
            crt=crt,
            now=now_sec,
            creation_minutes_west=creation_west,
            current_minutes_west=local_minutes_west_for_stamp(now_sec),
            rollover_hour=rollover,
        )

    def _today_due_index(self, now_sec: int) -> int:
        # Only the timing tests call this now; production goes through
        # _search_context() / _timing(). Kept as the small public-ish probe.
        with self._connect() as conn:
            return self._timing(conn, now_sec).days_elapsed

    def _insert_graves(self, conn: sqlite3.Connection, oids: list[int], grave_type: int) -> None:
        if not oids:
            return
        conn.executemany(
            "INSERT OR IGNORE INTO graves (oid, type, usn) VALUES (?, ?, -1)",
            [(oid, grave_type) for oid in oids],
        )

    def _search_context(self) -> SearchContext:
        now_sec = int(time.time())
        with self._connect() as conn:
            timing = self._timing(conn, now_sec)
        return SearchContext(
            now_sec=now_sec,
            due_day_index=timing.days_elapsed,
            next_day_at=timing.next_day_at,
        )

    def _next_new_due(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(due), 0) AS max_due FROM cards WHERE queue = 0"
        ).fetchone()
        return int(row["max_due"] or 0) + 1
