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

# Anki-written ``notes.csum`` values. Never derive these from the CLI's own
# checksum — they are the oracle. ``CSUM_TEST``/``CSUM_KYOU`` are rslib's own
# vectors (rslib/src/notes/mod.rs::test_field_checksum); the rest are literals
# for the stripped text noted alongside (rslib ``field_checksum`` runs over
# ``strip_html_preserving_media_filenames``: tags dropped, media filenames
# kept, entities decoded all-or-nothing).
CSUM_Q = 3_272_961_536  # "Q"
CSUM_A = 2_264_392_759  # "a"
CSUM_B = 3_923_189_598  # "b"
CSUM_F1 = 2_294_263_196  # "F1"
CSUM_CAPITAL_A = 1_842_171_106  # "A"
CSUM_Q_AMP_A = 2_136_065_763  # "Q&A" ("Q&amp;A" decoded)
CSUM_A_SPACE_B = 2_109_598_005  # "a b" ("a&nbsp;b" decoded)
CSUM_IMG_X_PNG = 2_926_313_888  # " x.png "
CSUM_IMG_Y_JPG = 1_649_267_424  # " y.jpg "
CSUM_IMG_Z_GIF = 2_472_787_776  # " z.gif "
CSUM_AUDIO_A_MP3 = 1_622_132_994  # " a.mp3 "
CSUM_OBJECT_O_SWF = 3_231_323_113  # " o.swf "
CSUM_VIDEO_V_MP4 = 985_886_235  # " v.mp4 "
CSUM_TEST = 2_840_236_005  # rslib vector: "test"
CSUM_KYOU = 1_464_653_051  # rslib vector: "今日"
# Strict-decode cases: a malformed entity makes htmlescape keep the ENTIRE
# original string, so these hash the literal markup text.
CSUM_R_AMPD_KEPT = 920_718_140  # "R&amp;D & more" kept (bare '&')
CSUM_AMPX_KEPT = 1_565_819_978  # "&ampx" kept (no-semicolon form)
CSUM_NBSP_NOSEMI_KEPT = 4_255_297_983  # "a&nbsp b" kept (no-semicolon form)
CSUM_CHECK_KEPT = 1_226_151_698  # "&check;" kept (HTML5-only name)
CSUM_HEX_UPPER_X_KEPT = 621_652_861  # "&#X41;" kept (uppercase X errs)
CSUM_SURROGATE_KEPT = 750_384_503  # "&#xD800;" kept (surrogate code point)
CSUM_BOGUS_KEPT = 2_006_993_251  # "&bogus;" kept (unknown entity)
CSUM_BAD_NUM_KEPT = 2_149_909_200  # "&#32a;" kept (malformed numeric)


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
