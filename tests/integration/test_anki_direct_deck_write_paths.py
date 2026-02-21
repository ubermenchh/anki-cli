from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, cast

import betterproto
import pytest

import anki_cli.db.anki_direct as direct_mod
from anki_cli.db.anki_direct import AnkiDirectReadStore
from anki_cli.proto.anki.decks import DeckCommon, DeckKindContainer, DeckNormal


def _make_store(
    tmp_path: Path,
    *,
    include_default: bool = True,
) -> tuple[AnkiDirectReadStore, Path]:
    db_path = tmp_path / "collection.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE decks (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            mtime_secs INTEGER NOT NULL,
            usn INTEGER NOT NULL,
            common BLOB NOT NULL,
            kind BLOB NOT NULL
        );

        CREATE TABLE notes (
            id INTEGER PRIMARY KEY
        );

        CREATE TABLE cards (
            id INTEGER PRIMARY KEY,
            nid INTEGER NOT NULL,
            did INTEGER NOT NULL
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

    if include_default:
        _insert_deck(db_path, deck_id=1, name="Default")

    return AnkiDirectReadStore(db_path), db_path


def _common_blob() -> bytes:
    return bytes(DeckCommon())


def _kind_blob(*, config_id: int = 1, description: str = "") -> bytes:
    return bytes(DeckKindContainer(normal=DeckNormal(config_id=config_id, description=description)))


def _insert_deck(
    db_path: Path,
    *,
    deck_id: int,
    name: str,
    mtime_secs: int = 1,
    usn: int = 0,
    config_id: int = 1,
    description: str = "",
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO decks (id, name, mtime_secs, usn, common, kind)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (deck_id, name, mtime_secs, usn, _common_blob(), 
        _kind_blob(config_id=config_id, description=description)),
    )
    conn.commit()
    conn.close()


def _insert_note(db_path: Path, *, note_id: int) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO notes (id) VALUES (?)", (note_id,))
    conn.commit()
    conn.close()


def _insert_card(db_path: Path, *, card_id: int, note_id: int, deck_id: int) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO cards (id, nid, did) VALUES (?, ?, ?)", (card_id, note_id, deck_id))
    conn.commit()
    conn.close()


def _deck_row(db_path: Path, deck_id: int) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT id, name, mtime_secs, usn, common, kind
        FROM decks
        WHERE id = ?
        """,
        (deck_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    return {k: row[k] for k in row.keys()}


def _deck_names(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT name FROM decks ORDER BY id").fetchall()
    conn.close()
    return [str(row[0]) for row in rows]


def _note_ids(db_path: Path) -> list[int]:
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT id FROM notes ORDER BY id").fetchall()
    conn.close()
    return [int(row[0]) for row in rows]


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


def _kind_info(kind_blob: bytes) -> tuple[str, int | None, str | None]:
    kind = DeckKindContainer().parse(kind_blob)
    kind_name, kind_msg = betterproto.which_one_of(kind, "kind")
    if kind_name == "normal" and kind_msg is not None:
        return kind_name, int(kind_msg.config_id), str(kind_msg.description)
    return kind_name, None, None


def test_write_deck_creates_from_template_and_applies_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_700_000_000)

    result = store.write_deck(
        name="  New Deck  ",
        config_id=5,
        description="hello",
    )

    assert result["deck"] == "New Deck"
    assert result["created"] is True
    deck_id = int(cast(int | str, result["id"]))
    row = _deck_row(db_path, deck_id)
    assert row["name"] == "New Deck"
    assert row["mtime_secs"] == 1_700_000_000
    assert row["usn"] == -1

    kind_name, config_id, description = _kind_info(bytes(row["kind"]))
    assert (kind_name, config_id, description) == ("normal", 5, "hello")


def test_write_deck_updates_existing_by_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    _insert_deck(db_path, deck_id=2, name="Work", config_id=1, description="old")
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_700_000_000)

    result = store.write_deck(name="Work", config_id=7, description="updated")
    assert result == {"deck": "Work", "id": 2, "created": False, "updated": True}

    row = _deck_row(db_path, 2)
    assert row["name"] == "Work"
    assert row["mtime_secs"] == 1_700_000_000
    assert row["usn"] == -1

    kind_name, config_id, description = _kind_info(bytes(row["kind"]))
    assert (kind_name, config_id, description) == ("normal", 7, "updated")


def test_write_deck_updates_existing_by_id_and_renames(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    _insert_deck(db_path, deck_id=20, name="OldName", config_id=3, description="x")
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_700_000_000)

    result = store.write_deck(name="Renamed", deck_id=20)
    assert result == {"deck": "Renamed", "id": 20, "created": False, "updated": True}

    row = _deck_row(db_path, 20)
    assert row["name"] == "Renamed"
    kind_name, config_id, description = _kind_info(bytes(row["kind"]))
    assert (kind_name, config_id, description) == ("normal", 3, "x")


def test_write_deck_missing_deck_id_raises_lookup_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, _db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    with pytest.raises(LookupError, match="Deck not found"):
        store.write_deck(name="Any", deck_id=999)


def test_write_deck_without_any_template_row_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, _db_path = _make_store(tmp_path, include_default=False)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    with pytest.raises(RuntimeError, match="No deck template row available"):
        store.write_deck(name="FreshDeck")


def test_rename_deck_subtree_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    _insert_deck(db_path, deck_id=10, name="Base")
    _insert_deck(db_path, deck_id=11, name="Base::Child")
    _insert_deck(db_path, deck_id=12, name="Base::Child::Leaf")
    _insert_deck(db_path, deck_id=13, name="Other")
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_700_000_000)

    result = store.rename_deck(old_name="Base", new_name="Renamed")
    assert result["from"] == "Base"
    assert result["to"] == "Renamed"
    assert result["renamed_decks"] == 3

    assert _deck_names(db_path) == [
        "Default",
        "Renamed",
        "Renamed::Child",
        "Renamed::Child::Leaf",
        "Other",
    ]
    assert _deck_row(db_path, 10)["usn"] == -1
    assert _deck_row(db_path, 11)["usn"] == -1
    assert _deck_row(db_path, 12)["usn"] == -1


def test_rename_deck_conflict_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    _insert_deck(db_path, deck_id=10, name="Base")
    _insert_deck(db_path, deck_id=20, name="Taken")
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    with pytest.raises(ValueError, match="Target deck path already exists"):
        store.rename_deck(old_name="Base", new_name="Taken")


def test_rename_deck_same_name_is_noop(tmp_path: Path) -> None:
    store, _db_path = _make_store(tmp_path)

    assert store.rename_deck(old_name="Default", new_name="Default") == {
        "from": "Default",
        "to": "Default",
        "renamed_decks": 0,
        "unchanged": True,
        "items": [],
    }


def test_rename_deck_missing_source_raises_lookup_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, _db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    with pytest.raises(LookupError, match="Deck not found"):
        store.rename_deck(old_name="Missing", new_name="NewName")


def test_delete_deck_missing_returns_not_deleted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, _db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    assert store.delete_deck("Missing") == {
        "deck": "Missing",
        "deleted": False,
        "deleted_decks": 0,
        "deleted_notes": 0,
        "deleted_cards": 0,
    }


def test_delete_deck_subtree_deletes_cards_conditional_notes_and_graves(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    _insert_deck(db_path, deck_id=10, name="Parent")
    _insert_deck(db_path, deck_id=11, name="Parent::Child")
    _insert_deck(db_path, deck_id=12, name="Other")

    _insert_note(db_path, note_id=100)
    _insert_note(db_path, note_id=101)
    _insert_note(db_path, note_id=102)
    _insert_note(db_path, note_id=103)

    # Note 100 spans in-scope and out-of-scope decks; it should survive.
    _insert_card(db_path, card_id=1000, note_id=100, deck_id=10)
    _insert_card(db_path, card_id=1001, note_id=100, deck_id=12)

    # Notes 101/102 are fully in-scope; they should be deleted.
    _insert_card(db_path, card_id=1002, note_id=101, deck_id=11)
    _insert_card(db_path, card_id=1003, note_id=102, deck_id=10)

    # Out-of-scope control note/card.
    _insert_card(db_path, card_id=1004, note_id=103, deck_id=12)

    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    result = store.delete_deck("Parent")
    assert result == {
        "deck": "Parent",
        "deleted": True,
        "deleted_decks": 2,
        "deleted_notes": 2,
        "deleted_cards": 3,
    }

    assert _deck_names(db_path) == ["Default", "Other"]
    assert _note_ids(db_path) == [100, 103]
    assert _card_ids(db_path) == [1001, 1004]
    assert set(_grave_rows(db_path)) == {
        (1000, 0, -1),
        (1002, 0, -1),
        (1003, 0, -1),
        (101, 1, -1),
        (102, 1, -1),
        (10, 2, -1),
        (11, 2, -1),
    }