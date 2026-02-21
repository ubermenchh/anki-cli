from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

import anki_cli.db.anki_direct as direct_mod
from anki_cli.db.anki_direct import AnkiDirectReadStore


def _make_store(tmp_path: Path) -> tuple[AnkiDirectReadStore, Path]:
    db_path = tmp_path / "collection.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE col (
            crt INTEGER NOT NULL
        );

        CREATE TABLE notes (
            id INTEGER PRIMARY KEY,
            tags TEXT NOT NULL,
            mod INTEGER NOT NULL,
            usn INTEGER NOT NULL
        );

        CREATE TABLE cards (
            id INTEGER PRIMARY KEY,
            type INTEGER NOT NULL,
            queue INTEGER NOT NULL,
            mod INTEGER NOT NULL,
            usn INTEGER NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO col (crt) VALUES (0)")
    conn.commit()
    conn.close()

    return AnkiDirectReadStore(db_path), db_path


def _insert_note(db_path: Path, *, note_id: int, tags: str, mod: int = 1, usn: int = 0) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO notes (id, tags, mod, usn) VALUES (?, ?, ?, ?)",
        (note_id, tags, mod, usn),
    )
    conn.commit()
    conn.close()


def _insert_card(
    db_path: Path,
    *,
    card_id: int,
    card_type: int,
    queue: int,
    mod: int = 1,
    usn: int = 0,
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO cards (id, type, queue, mod, usn) VALUES (?, ?, ?, ?, ?)",
        (card_id, card_type, queue, mod, usn),
    )
    conn.commit()
    conn.close()


def _note_row(db_path: Path, note_id: int) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, tags, mod, usn FROM notes WHERE id = ?",
        (note_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    return {k: row[k] for k in row.keys()}


def _card_row(db_path: Path, card_id: int) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, type, queue, mod, usn FROM cards WHERE id = ?",
        (card_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    return {k: row[k] for k in row.keys()}


def test_suspend_cards_updates_existing_cards_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_700_000_000)

    _insert_card(db_path, card_id=1, card_type=0, queue=0)
    _insert_card(db_path, card_id=2, card_type=2, queue=2)
    _insert_card(db_path, card_id=3, card_type=3, queue=1)

    result = store.suspend_cards([3, 1, 3, 999, 0, -1])

    assert result["updated"] == 2
    assert result["suspended"] == 2
    assert set(cast(list[int], result["card_ids"])) == {1, 3}

    assert _card_row(db_path, 1)["queue"] == -1
    assert _card_row(db_path, 3)["queue"] == -1
    assert _card_row(db_path, 1)["mod"] == 1_700_000_000
    assert _card_row(db_path, 3)["mod"] == 1_700_000_000
    assert _card_row(db_path, 1)["usn"] == -1
    assert _card_row(db_path, 3)["usn"] == -1

    # Untouched card remains unchanged.
    assert _card_row(db_path, 2)["queue"] == 2
    assert _card_row(db_path, 2)["usn"] == 0


def test_unsuspend_cards_restores_queue_by_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_700_000_000)

    _insert_card(db_path, card_id=11, card_type=0, queue=-1)
    _insert_card(db_path, card_id=12, card_type=2, queue=-1)
    _insert_card(db_path, card_id=13, card_type=3, queue=-1)
    _insert_card(db_path, card_id=14, card_type=1, queue=-1)

    result = store.unsuspend_cards([14, 13, 12, 11, 999])

    assert result["updated"] == 4
    assert result["unsuspended"] == 4
    assert set(cast(list[int], result["card_ids"])) == {11, 12, 13, 14}

    assert _card_row(db_path, 11)["queue"] == 0
    assert _card_row(db_path, 12)["queue"] == 2
    assert _card_row(db_path, 13)["queue"] == 3
    assert _card_row(db_path, 14)["queue"] == 1


def test_suspend_and_unsuspend_return_noop_when_no_existing_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, _db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    assert store.suspend_cards([999, 1000]) == {"updated": 0, "card_ids": []}
    assert store.unsuspend_cards([999, 1000]) == {"updated": 0, "card_ids": []}
    assert store.suspend_cards([]) == {"updated": 0, "card_ids": []}
    assert store.unsuspend_cards([]) == {"updated": 0, "card_ids": []}


def test_add_tags_merges_case_insensitive_and_updates_existing_notes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_700_000_000)

    _insert_note(db_path, note_id=10, tags=" alpha Old ", mod=10)
    _insert_note(db_path, note_id=20, tags=" gamma ", mod=20)
    _insert_note(db_path, note_id=30, tags=" untouched ", mod=30)

    result = store.add_tags(
        note_ids=[20, 10, 20, 999, 0],
        tags=["beta", "ALPHA", "beta"],
    )

    assert result["updated"] == 2
    assert set(cast(list[int], result["note_ids"])) == {10, 20}
    assert result["tags"] == ["beta", "ALPHA", "beta"]

    note10 = _note_row(db_path, 10)
    note20 = _note_row(db_path, 20)
    note30 = _note_row(db_path, 30)

    assert note10["tags"] == " ALPHA beta Old "
    assert note20["tags"] == " ALPHA beta gamma "
    assert note30["tags"] == " untouched "

    assert note10["mod"] == 1_700_000_000
    assert note20["mod"] == 1_700_000_000
    assert note10["usn"] == -1
    assert note20["usn"] == -1
    assert note30["mod"] == 30
    assert note30["usn"] == 0


def test_add_tags_returns_noop_for_missing_ids_or_empty_tags(tmp_path: Path) -> None:
    store, _db_path = _make_store(tmp_path)

    assert store.add_tags([], ["x"]) == {"updated": 0, "note_ids": [], "tags": ["x"]}
    assert store.add_tags([1], []) == {"updated": 0, "note_ids": [], "tags": []}


def test_remove_tags_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_700_000_000)

    _insert_note(db_path, note_id=1, tags=" A b c ", mod=10)
    _insert_note(db_path, note_id=2, tags=" x y ", mod=20)
    _insert_note(db_path, note_id=3, tags=" keep ", mod=30)

    result = store.remove_tags(note_ids=[2, 1, 999], tags=["B", "x"])

    assert result["updated"] == 2
    assert set(cast(list[int], result["note_ids"])) == {1, 2}
    assert result["tags"] == ["B", "x"]

    note1 = _note_row(db_path, 1)
    note2 = _note_row(db_path, 2)
    note3 = _note_row(db_path, 3)

    assert note1["tags"] == " A c "
    assert note2["tags"] == " y "
    assert note3["tags"] == " keep "
    assert note1["mod"] == 1_700_000_000
    assert note2["mod"] == 1_700_000_000
    assert note1["usn"] == -1
    assert note2["usn"] == -1


def test_remove_tags_returns_noop_for_missing_ids_or_empty_tags(tmp_path: Path) -> None:
    store, _db_path = _make_store(tmp_path)

    assert store.remove_tags([], ["x"]) == {"updated": 0, "note_ids": [], "tags": ["x"]}
    assert store.remove_tags([1], []) == {"updated": 0, "note_ids": [], "tags": []}


def test_rename_tag_is_exact_case_sensitive_and_deduplicates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_700_000_000)

    _insert_note(db_path, note_id=1, tags=" foo baz ", mod=10)
    _insert_note(db_path, note_id=2, tags=" bar foo ", mod=20)
    _insert_note(db_path, note_id=3, tags=" Foo ", mod=30)

    result = store.rename_tag(old_tag=" foo ", new_tag=" bar ")

    assert result == {"from": "foo", "to": "bar", "updated": 2}

    note1 = _note_row(db_path, 1)
    note2 = _note_row(db_path, 2)
    note3 = _note_row(db_path, 3)

    assert note1["tags"] == " bar baz "
    assert note2["tags"] == " bar "
    assert note3["tags"] == " Foo "  # unchanged: exact-case match only

    assert note1["mod"] == 1_700_000_000
    assert note2["mod"] == 1_700_000_000
    assert note3["mod"] == 30
    assert note1["usn"] == -1
    assert note2["usn"] == -1
    assert note3["usn"] == 0


def test_rename_tag_requires_both_values(tmp_path: Path) -> None:
    store, _db_path = _make_store(tmp_path)

    with pytest.raises(ValueError, match="Both tags are required"):
        store.rename_tag(old_tag=" ", new_tag="x")

    with pytest.raises(ValueError, match="Both tags are required"):
        store.rename_tag(old_tag="x", new_tag=" ")