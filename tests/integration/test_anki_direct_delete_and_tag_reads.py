from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from anki_cli.db.anki_direct import AnkiDirectReadStore


def _make_store(tmp_path: Path) -> tuple[AnkiDirectReadStore, Path]:
    db_path = tmp_path / "collection.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE notes (
            id INTEGER PRIMARY KEY,
            tags TEXT NOT NULL
        );

        CREATE TABLE cards (
            id INTEGER PRIMARY KEY
        );

        CREATE TABLE graves (
            oid INTEGER NOT NULL,
            type INTEGER NOT NULL,
            usn INTEGER NOT NULL,
            PRIMARY KEY (oid, type)
        );
        """
    )
    conn.commit()
    conn.close()

    return AnkiDirectReadStore(db_path), db_path


def _insert_note(db_path: Path, *, note_id: int, tags: str) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO notes (id, tags) VALUES (?, ?)", (note_id, tags))
    conn.commit()
    conn.close()


def _insert_card(db_path: Path, *, card_id: int) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO cards (id) VALUES (?)", (card_id,))
    conn.commit()
    conn.close()


def _card_ids(db_path: Path) -> list[int]:
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT id FROM cards ORDER BY id").fetchall()
    conn.close()
    return [int(row[0]) for row in rows]


def _grave_rows(db_path: Path) -> list[tuple[int, int, int]]:
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT oid, type, usn FROM graves ORDER BY oid, type").fetchall()
    conn.close()
    return [(int(oid), int(gtype), int(usn)) for (oid, gtype, usn) in rows]


def test_delete_card_non_positive_returns_deleted_false(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)
    _insert_card(db_path, card_id=10)

    assert store.delete_card(0) == {"card_id": 0, "deleted": False}
    assert store.delete_card(-7) == {"card_id": -7, "deleted": False}

    assert _card_ids(db_path) == [10]
    assert _grave_rows(db_path) == []


def test_delete_card_missing_returns_deleted_false(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    _insert_card(db_path, card_id=10)

    assert store.delete_card(999) == {"card_id": 999, "deleted": False}
    assert _card_ids(db_path) == [10]
    assert _grave_rows(db_path) == []


def test_delete_card_existing_deletes_card_and_inserts_grave(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    _insert_card(db_path, card_id=10)
    _insert_card(db_path, card_id=20)

    assert store.delete_card(10) == {"card_id": 10, "deleted": True}
    assert _card_ids(db_path) == [20]
    assert _grave_rows(db_path) == [(10, 0, -1)]


def test_delete_card_second_call_is_noop_and_grave_not_duplicated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    _insert_card(db_path, card_id=42)

    assert store.delete_card(42) == {"card_id": 42, "deleted": True}
    assert store.delete_card(42) == {"card_id": 42, "deleted": False}
    assert _card_ids(db_path) == []
    assert _grave_rows(db_path) == [(42, 0, -1)]


def test_get_tags_returns_unique_sorted_case_insensitive(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)

    _insert_note(db_path, note_id=1, tags=" Zulu alpha ")
    _insert_note(db_path, note_id=2, tags=" beta ")
    _insert_note(db_path, note_id=3, tags=" alpha ")

    assert store.get_tags() == ["alpha", "beta", "Zulu"]


def test_get_tag_counts_counts_occurrences(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)

    _insert_note(db_path, note_id=1, tags=" alpha beta ")
    _insert_note(db_path, note_id=2, tags=" beta gamma ")
    _insert_note(db_path, note_id=3, tags=" gamma ")

    assert store.get_tag_counts() == [
        {"tag": "alpha", "count": 1},
        {"tag": "beta", "count": 2},
        {"tag": "gamma", "count": 2},
    ]


def test_get_tag_counts_treats_case_variants_as_distinct(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)

    _insert_note(db_path, note_id=1, tags=" Foo ")
    _insert_note(db_path, note_id=2, tags=" foo Foo ")

    counts = {item["tag"]: item["count"] for item in store.get_tag_counts()}
    assert counts == {"Foo": 2, "foo": 1}


def test_get_tags_and_counts_empty_db_return_empty(tmp_path: Path) -> None:
    store, _db_path = _make_store(tmp_path)

    assert store.get_tags() == []
    assert store.get_tag_counts() == []