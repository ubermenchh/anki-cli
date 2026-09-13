"""Regression tests for #24: refuse legacy (schema < 18) collections up front."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from anki_cli.db.anki_direct import (
    MIN_SUPPORTED_SCHEMA_VERSION,
    AnkiDirectReadStore,
    UnsupportedCollectionError,
)
from tests.integration.conftest import COL_TABLE_SQL, insert_col_row


def _legacy_collection(tmp_path: Path, *, ver: int) -> Path:
    """Schema-11 shape: everything lives in col as JSON; no decks/notetypes tables."""
    db = tmp_path / "collection.anki2"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE col (
            id integer PRIMARY KEY, crt integer NOT NULL, mod integer NOT NULL,
            scm integer NOT NULL, ver integer NOT NULL, dty integer NOT NULL,
            usn integer NOT NULL, ls integer NOT NULL, conf text NOT NULL,
            models text NOT NULL, decks text NOT NULL, dconf text NOT NULL, tags text NOT NULL
        );
        CREATE TABLE cards (id integer PRIMARY KEY);
        """)
    conn.execute(
        "INSERT INTO col VALUES (1, 0, 0, 0, ?, 0, 0, 0, '{}', '{}', '{}', '{}', '{}')",
        (ver,),
    )
    conn.commit()
    conn.close()
    return db


def test_schema_11_collection_is_refused_with_upgrade_hint(tmp_path: Path) -> None:
    db = _legacy_collection(tmp_path, ver=11)

    with pytest.raises(UnsupportedCollectionError) as excinfo:
        AnkiDirectReadStore(db)

    message = str(excinfo.value)
    assert "schema 11" in message
    assert str(MIN_SUPPORTED_SCHEMA_VERSION) in message
    assert "2.1.50" in message
    assert str(db.resolve()) in message


@pytest.mark.parametrize("ver", [MIN_SUPPORTED_SCHEMA_VERSION, MIN_SUPPORTED_SCHEMA_VERSION + 1])
def test_supported_schema_versions_construct(tmp_path: Path, ver: int) -> None:
    db = _legacy_collection(tmp_path, ver=ver)

    store = AnkiDirectReadStore(db)

    assert store.db_path == db.resolve()


def test_boundary_just_below_minimum_is_refused(tmp_path: Path) -> None:
    db = _legacy_collection(tmp_path, ver=MIN_SUPPORTED_SCHEMA_VERSION - 1)

    with pytest.raises(UnsupportedCollectionError):
        AnkiDirectReadStore(db)


def test_real_col_ddl_from_conftest_is_accepted(tmp_path: Path) -> None:
    db = tmp_path / "collection.anki2"
    conn = sqlite3.connect(str(db))
    conn.executescript(COL_TABLE_SQL)
    insert_col_row(conn, crt=0)
    conn.commit()
    conn.close()

    AnkiDirectReadStore(db)  # must not raise


def _sqlite_with(script: str, rows: list[tuple[str, tuple]] | None = None):
    def build(path: Path) -> Path:
        conn = sqlite3.connect(str(path))
        conn.executescript(script)
        for sql, params in rows or []:
            conn.execute(sql, params)
        conn.commit()
        conn.close()
        return path

    return build


def _text_file(path: Path) -> Path:
    path.write_text("this is not a database")
    return path


@pytest.mark.parametrize(
    ("label", "build"),
    [
        ("no_col_table", _sqlite_with("CREATE TABLE dummy (id INTEGER PRIMARY KEY)")),
        ("col_without_ver", _sqlite_with("CREATE TABLE col (crt INTEGER NOT NULL)")),
        ("col_empty", _sqlite_with("CREATE TABLE col (ver)")),
        (
            "ver_null",
            _sqlite_with("CREATE TABLE col (ver)", [("INSERT INTO col VALUES (?)", (None,))]),
        ),
        (
            "ver_text",
            _sqlite_with("CREATE TABLE col (ver)", [("INSERT INTO col VALUES (?)", ("abc",))]),
        ),
        ("not_sqlite", _text_file),
    ],
)
def test_files_without_a_readable_version_are_left_alone(tmp_path: Path, label: str, build) -> None:
    """Only the version is policed here; other problems surface from later queries."""
    db = build(tmp_path / f"{label}.anki2")

    store = AnkiDirectReadStore(db)  # must not raise

    # ... and "later queries" really do reject the file on their own terms.
    with pytest.raises(sqlite3.Error), store._connect() as conn:
        conn.execute("SELECT id FROM decks").fetchall()


@pytest.mark.parametrize(
    "profile_name",
    [
        "Uwe's Deck#1",
        pytest.param(
            "Deck?1",
            marks=pytest.mark.skipif(
                sys.platform == "win32", reason="'?' is invalid in NTFS names"
            ),
        ),
    ],
)
def test_guard_handles_paths_with_uri_delimiters(tmp_path: Path, profile_name: str) -> None:
    """'#' and '?' terminate the path in SQLite's URI parser; the guard must still
    see the real file (and must not create a stray truncated-path file next to it)."""
    profile = tmp_path / profile_name
    profile.mkdir()
    db = _legacy_collection(profile, ver=11)

    with pytest.raises(UnsupportedCollectionError, match="schema 11"):
        AnkiDirectReadStore(db)

    assert sorted(p.name for p in tmp_path.iterdir()) == [profile.name]


def test_guard_works_on_a_wal_collection_without_shm(tmp_path: Path) -> None:
    """Real collections are WAL; after a clean close no -wal/-shm exists."""
    db = _legacy_collection(tmp_path, ver=11)
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.commit()
    conn.close()
    assert not (tmp_path / "collection.anki2-shm").exists()

    with pytest.raises(UnsupportedCollectionError):
        AnkiDirectReadStore(db)


def test_guard_does_not_lock_the_file(tmp_path: Path) -> None:
    """The version probe opens read-only and closes; a writer must not be blocked."""
    db = _legacy_collection(tmp_path, ver=MIN_SUPPORTED_SCHEMA_VERSION)
    AnkiDirectReadStore(db)

    conn = sqlite3.connect(str(db), timeout=0.1)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE col SET mod = 1")
    conn.commit()
    conn.close()
