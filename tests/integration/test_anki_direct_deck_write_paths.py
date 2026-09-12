from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, cast

import betterproto
import pytest

import anki_cli.db.anki_direct as direct_mod
from anki_cli.db.anki_direct import AnkiDirectReadStore
from anki_cli.proto.anki.decks import (
    DeckCommon,
    DeckFiltered,
    DeckFilteredSearchTerm,
    DeckKindContainer,
    DeckNormal,
)
from tests.integration.conftest import COL_TABLE_SQL, insert_col_row


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
            did INTEGER NOT NULL,
            mod INTEGER NOT NULL DEFAULT 0,
            usn INTEGER NOT NULL DEFAULT 0,
            type INTEGER NOT NULL DEFAULT 0,
            queue INTEGER NOT NULL DEFAULT 0,
            due INTEGER NOT NULL DEFAULT 0,
            odue INTEGER NOT NULL DEFAULT 0,
            odid INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE graves (
            oid INTEGER NOT NULL,
            type INTEGER NOT NULL,
            usn INTEGER NOT NULL,
            PRIMARY KEY (oid, type)
        );
        """
    )
    conn.executescript(COL_TABLE_SQL)
    insert_col_row(conn, crt=0)
    conn.commit()
    conn.close()

    if include_default:
        _insert_deck(db_path, deck_id=1, name="Default")

    return AnkiDirectReadStore(db_path), db_path


def _common_blob() -> bytes:
    return bytes(DeckCommon())


def _kind_blob(*, config_id: int = 1, description: str = "") -> bytes:
    return bytes(DeckKindContainer(normal=DeckNormal(config_id=config_id, description=description)))


def _filtered_kind_blob() -> bytes:
    return bytes(
        DeckKindContainer(
            filtered=DeckFiltered(
                reschedule=True,
                search_terms=[DeckFilteredSearchTerm(search="is:due", limit=100, order=0)],
            )
        )
    )


def _insert_deck(
    db_path: Path,
    *,
    deck_id: int,
    name: str,
    mtime_secs: int = 1,
    usn: int = 0,
    config_id: int = 1,
    description: str = "",
    filtered: bool = False,
) -> None:
    kind = (
        _filtered_kind_blob()
        if filtered
        else _kind_blob(config_id=config_id, description=description)
    )
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO decks (id, name, mtime_secs, usn, common, kind)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (deck_id, name, mtime_secs, usn, _common_blob(), kind),
    )
    conn.commit()
    conn.close()


def _insert_note(db_path: Path, *, note_id: int) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO notes (id) VALUES (?)", (note_id,))
    conn.commit()
    conn.close()


def _insert_card(
    db_path: Path,
    *,
    card_id: int,
    note_id: int,
    deck_id: int,
    type_: int = 0,
    queue: int = 0,
    due: int = 0,
    odid: int = 0,
    odue: int = 0,
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO cards (id, nid, did, type, queue, due, odid, odue)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (card_id, note_id, deck_id, type_, queue, due, odid, odue),
    )
    conn.commit()
    conn.close()


def _card_row(db_path: Path, card_id: int) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, nid, did, mod, usn, type, queue, due, odid, odue FROM cards WHERE id = ?",
        (card_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    return dict(row)


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
        "returned_cards": 0,
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
        "returned_cards": 0,
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


def test_delete_deck_refuses_default_deck(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    _insert_deck(db_path, deck_id=2, name="Default::Sub")
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    with pytest.raises(ValueError, match="Cannot delete the Default deck"):
        store.delete_deck("Default")

    # Nothing was touched, including the child deck.
    assert _deck_names(db_path) == ["Default", "Default::Sub"]


def test_delete_deck_refuses_unknown_kind_and_leaves_collection_untouched(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A deck whose kind blob decodes to neither normal nor filtered must fail closed."""
    store, db_path = _make_store(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO decks (id, name, mtime_secs, usn, common, kind) VALUES (?, ?, 1, 0, ?, ?)",
        (10, "Broken", _common_blob(), b""),
    )
    conn.commit()
    conn.close()
    _insert_note(db_path, note_id=100)
    _insert_card(db_path, card_id=1000, note_id=100, deck_id=10)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    with pytest.raises(ValueError, match="unknown kind"):
        store.delete_deck("Broken")

    assert _deck_names(db_path) == ["Default", "Broken"]
    assert _card_ids(db_path) == [1000]
    assert _note_ids(db_path) == [100]
    assert _grave_rows(db_path) == []


def test_delete_deck_with_malformed_child_rolls_back_the_whole_subtree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A valid parent plus a malformed child: nothing in the subtree may be deleted."""
    store, db_path = _make_store(tmp_path)
    _insert_deck(db_path, deck_id=10, name="Parent")
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO decks (id, name, mtime_secs, usn, common, kind) VALUES (?, ?, 1, 0, ?, ?)",
        (11, "Parent::Broken", _common_blob(), b""),
    )
    conn.commit()
    conn.close()
    _insert_note(db_path, note_id=100)
    _insert_card(db_path, card_id=1000, note_id=100, deck_id=10)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    with pytest.raises(ValueError, match="unknown kind"):
        store.delete_deck("Parent")

    assert _deck_names(db_path) == ["Default", "Parent", "Parent::Broken"]
    assert _card_ids(db_path) == [1000]
    assert _note_ids(db_path) == [100]
    assert _grave_rows(db_path) == []


def test_delete_filtered_deck_returns_cards_home_instead_of_deleting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression for #16: a filtered deck only borrows cards."""
    store, db_path = _make_store(tmp_path)
    _insert_deck(db_path, deck_id=10, name="Home")
    _insert_deck(db_path, deck_id=555, name="Cram", filtered=True)
    _insert_note(db_path, note_id=100)
    _insert_note(db_path, note_id=101)
    _insert_note(db_path, note_id=102)
    _insert_note(db_path, note_id=103)

    # Review card on loan from Home.
    _insert_card(
        db_path,
        card_id=1000,
        note_id=100,
        deck_id=555,
        type_=2,
        queue=2,
        due=-5,
        odid=10,
        odue=19700,
    )
    # New card on loan; filtered decks reposition new cards via due.
    _insert_card(
        db_path,
        card_id=1001,
        note_id=101,
        deck_id=555,
        type_=0,
        queue=0,
        due=100001,
        odid=10,
        odue=42,
    )
    # Intraday learning card (epoch-seconds due) on loan.
    _insert_card(
        db_path,
        card_id=1002,
        note_id=102,
        deck_id=555,
        type_=1,
        queue=1,
        due=1_700_000_000,
        odid=10,
        odue=1_700_000_600,
    )
    # Suspended while in the filtered deck: must stay suspended when returned.
    _insert_card(
        db_path,
        card_id=1003,
        note_id=103,
        deck_id=555,
        type_=2,
        queue=-1,
        due=-5,
        odid=10,
        odue=19800,
    )
    # Card that lives in Home and is not on loan: untouched.
    _insert_card(db_path, card_id=1004, note_id=103, deck_id=10, type_=2, queue=2, due=19900)
    # New card repositioned by the filtered deck with odue == 0: "leave due alone".
    _insert_card(
        db_path,
        card_id=1005,
        note_id=103,
        deck_id=555,
        type_=0,
        queue=0,
        due=-100001,
        odid=10,
        odue=0,
    )
    # Buried while in the filtered deck: must keep queue -3.
    _insert_card(
        db_path,
        card_id=1006,
        note_id=103,
        deck_id=555,
        type_=2,
        queue=-3,
        due=-5,
        odid=10,
        odue=19850,
    )

    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_700_000_000)

    result = store.delete_deck("Cram")
    assert result == {
        "deck": "Cram",
        "deleted": True,
        "deleted_decks": 1,
        "deleted_notes": 0,
        "deleted_cards": 0,
        "returned_cards": 6,
    }

    assert _deck_names(db_path) == ["Default", "Home"]
    assert _note_ids(db_path) == [100, 101, 102, 103]
    assert _card_ids(db_path) == [1000, 1001, 1002, 1003, 1004, 1005, 1006]
    # Only the deck is graved; no card or note graves.
    assert _grave_rows(db_path) == [(555, 2, -1)]

    review = _card_row(db_path, 1000)
    assert (review["did"], review["due"], review["odid"], review["odue"]) == (10, 19700, 0, 0)
    assert review["queue"] == 2
    assert (review["mod"], review["usn"]) == (1_700_000_000, -1)

    new = _card_row(db_path, 1001)
    assert (new["did"], new["due"], new["queue"]) == (10, 42, 0)

    learn = _card_row(db_path, 1002)
    assert (learn["did"], learn["due"], learn["queue"]) == (10, 1_700_000_600, 1)

    suspended = _card_row(db_path, 1003)
    assert (suspended["did"], suspended["due"], suspended["queue"]) == (10, 19800, -1)

    untouched = _card_row(db_path, 1004)
    assert (untouched["did"], untouched["due"], untouched["usn"]) == (10, 19900, 0)

    kept_due = _card_row(db_path, 1005)
    assert (kept_due["did"], kept_due["due"], kept_due["queue"], kept_due["odid"]) == (
        10,
        -100001,
        0,
        0,
    )

    buried = _card_row(db_path, 1006)
    assert (buried["did"], buried["due"], buried["queue"]) == (10, 19850, -3)


def test_delete_filtered_deck_restores_day_learn_queue_and_parks_stray_cards(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    _insert_deck(db_path, deck_id=10, name="Home")
    _insert_deck(db_path, deck_id=555, name="Cram", filtered=True)
    _insert_note(db_path, note_id=100)
    _insert_note(db_path, note_id=101)

    # Relearning card whose home due is a day index -> day-learn queue (3).
    _insert_card(
        db_path,
        card_id=1000,
        note_id=100,
        deck_id=555,
        type_=3,
        queue=1,
        due=-3,
        odid=10,
        odue=19701,
    )
    # Malformed: in a filtered deck but no home deck recorded.
    _insert_card(db_path, card_id=1001, note_id=101, deck_id=555, type_=2, queue=2, due=19700)

    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    result = store.delete_deck("Cram")
    assert result["returned_cards"] == 2
    assert result["deleted_cards"] == 0

    relearn = _card_row(db_path, 1000)
    assert (relearn["did"], relearn["due"], relearn["queue"]) == (10, 19701, 3)

    stray = _card_row(db_path, 1001)
    assert (stray["did"], stray["due"], stray["usn"]) == (1, 19700, -1)


def test_delete_normal_deck_also_deletes_its_cards_on_loan_to_a_filtered_deck(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    _insert_deck(db_path, deck_id=10, name="Home")
    _insert_deck(db_path, deck_id=555, name="Cram", filtered=True)
    _insert_note(db_path, note_id=100)
    _insert_note(db_path, note_id=101)

    _insert_card(db_path, card_id=1000, note_id=100, deck_id=10)
    # Owned by Home, currently sitting in Cram.
    _insert_card(db_path, card_id=1001, note_id=101, deck_id=555, odid=10, odue=5)

    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    result = store.delete_deck("Home")
    assert result == {
        "deck": "Home",
        "deleted": True,
        "deleted_decks": 1,
        "deleted_notes": 2,
        "deleted_cards": 2,
        "returned_cards": 0,
    }
    assert _deck_names(db_path) == ["Default", "Cram"]
    assert _card_ids(db_path) == []
    assert _note_ids(db_path) == []
    assert set(_grave_rows(db_path)) == {
        (1000, 0, -1),
        (1001, 0, -1),
        (100, 1, -1),
        (101, 1, -1),
        (10, 2, -1),
    }


def test_delete_subtree_with_filtered_child_returns_then_deletes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A filtered child inside a deleted normal subtree: cards go home first,
    then are deleted only if their home deck is also in scope."""
    store, db_path = _make_store(tmp_path)
    _insert_deck(db_path, deck_id=10, name="Parent")
    _insert_deck(db_path, deck_id=11, name="Parent::Cram", filtered=True)
    _insert_deck(db_path, deck_id=20, name="Elsewhere")
    _insert_note(db_path, note_id=100)
    _insert_note(db_path, note_id=101)

    # Home is Parent (in scope) -> deleted after return.
    _insert_card(db_path, card_id=1000, note_id=100, deck_id=11, odid=10, odue=7)
    # Home is Elsewhere (out of scope) -> survives, back in Elsewhere.
    _insert_card(db_path, card_id=1001, note_id=101, deck_id=11, odid=20, odue=9)

    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    result = store.delete_deck("Parent")
    assert result["deleted_decks"] == 2
    assert result["returned_cards"] == 2
    assert result["deleted_cards"] == 1
    assert result["deleted_notes"] == 1

    assert _deck_names(db_path) == ["Default", "Elsewhere"]
    assert _card_ids(db_path) == [1001]
    assert _note_ids(db_path) == [101]
    survivor = _card_row(db_path, 1001)
    assert (survivor["did"], survivor["due"], survivor["odid"]) == (20, 9, 0)
