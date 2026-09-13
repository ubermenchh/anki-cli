"""Regression tests for #24: refuse legacy (schema < 18) collections up front."""

from __future__ import annotations

import sqlite3
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
    conn.executescript(
        """
        CREATE TABLE col (
            id integer PRIMARY KEY, crt integer NOT NULL, mod integer NOT NULL,
            scm integer NOT NULL, ver integer NOT NULL, dty integer NOT NULL,
            usn integer NOT NULL, ls integer NOT NULL, conf text NOT NULL,
            models text NOT NULL, decks text NOT NULL, dconf text NOT NULL, tags text NOT NULL
        );
        CREATE TABLE cards (id integer PRIMARY KEY);
        """
    )
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


def test_files_without_a_readable_version_are_left_alone(tmp_path: Path) -> None:
    """Only the version is policed here; other problems surface from later queries."""
    no_col = tmp_path / "no_col.db"
    conn = sqlite3.connect(str(no_col))
    conn.execute("CREATE TABLE dummy (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    AnkiDirectReadStore(no_col)

    no_ver = tmp_path / "no_ver.db"
    conn = sqlite3.connect(str(no_ver))
    conn.execute("CREATE TABLE col (crt INTEGER NOT NULL)")
    conn.execute("INSERT INTO col (crt) VALUES (0)")
    conn.commit()
    conn.close()
    AnkiDirectReadStore(no_ver)

    not_sqlite = tmp_path / "text.anki2"
    not_sqlite.write_text("this is not a database")
    AnkiDirectReadStore(not_sqlite)


def test_guard_does_not_lock_the_file(tmp_path: Path) -> None:
    """The version probe opens read-only and closes; a writer must not be blocked."""
    db = _legacy_collection(tmp_path, ver=MIN_SUPPORTED_SCHEMA_VERSION)
    AnkiDirectReadStore(db)

    conn = sqlite3.connect(str(db), timeout=0.1)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE col SET mod = 1")
    conn.commit()
    conn.close()
