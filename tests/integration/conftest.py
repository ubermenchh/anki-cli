"""Shared scaffolding for direct-backend integration tests.

The per-file ``_make_store`` builders still own most of their schema (see #37 for
the plan to consolidate them); this module provides the pieces every write path
touches so they can't drift between files.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

# Anki schema-18 ``col`` table, verbatim. Every write in the direct backend bumps
# ``mod`` and schema changes bump ``scm``, so every fixture that exercises a
# mutator needs the real column set.
COL_TABLE_SQL = """
CREATE TABLE col (
    id integer PRIMARY KEY,
    crt integer NOT NULL,
    mod integer NOT NULL,
    scm integer NOT NULL,
    ver integer NOT NULL,
    dty integer NOT NULL,
    usn integer NOT NULL,
    ls integer NOT NULL,
    conf text NOT NULL,
    models text NOT NULL,
    decks text NOT NULL,
    dconf text NOT NULL,
    tags text NOT NULL
);
"""

# Baseline timestamps chosen so tests can assert "moved" without depending on
# the wall clock: negative, so no real or monkeypatched ``time.time()`` (even
# ``lambda: 0``) can collide with them.
COL_BASE_MOD_MS = -1
COL_BASE_SCM_MS = -2


def insert_col_row(conn: sqlite3.Connection, *, crt: int = 0) -> None:
    conn.execute(
        """
        INSERT INTO col (id, crt, mod, scm, ver, dty, usn, ls, conf, models, decks, dconf, tags)
        VALUES (1, ?, ?, ?, 18, 0, 0, 0, '{}', '{}', '{}', '{}', '{}')
        """,
        (crt, COL_BASE_MOD_MS, COL_BASE_SCM_MS),
    )


def col_row(db_path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT crt, mod, scm FROM col").fetchone()
    conn.close()
    assert row is not None
    return dict(row)


def assert_col_modified(db_path: Path, *, schema: bool = False) -> None:
    """Assert the last write bumped ``col.mod`` and, if ``schema``, ``col.scm``."""
    row = col_row(db_path)
    assert row["mod"] > COL_BASE_MOD_MS, "col.mod was not bumped; sync would see 'No changes'"
    if schema:
        assert row["scm"] > COL_BASE_SCM_MS, "col.scm was not bumped; schema change not recorded"
    else:
        assert row["scm"] == COL_BASE_SCM_MS, "col.scm moved on a non-schema change"


def assert_col_untouched(db_path: Path) -> None:
    row = col_row(db_path)
    assert row["mod"] == COL_BASE_MOD_MS
    assert row["scm"] == COL_BASE_SCM_MS
