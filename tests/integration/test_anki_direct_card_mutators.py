from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

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

        CREATE TABLE decks (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        );

        CREATE TABLE cards (
            id INTEGER PRIMARY KEY,
            did INTEGER NOT NULL,
            type INTEGER NOT NULL,
            queue INTEGER NOT NULL,
            due INTEGER NOT NULL,
            ivl INTEGER NOT NULL,
            factor INTEGER NOT NULL,
            reps INTEGER NOT NULL,
            lapses INTEGER NOT NULL,
            left INTEGER NOT NULL,
            odue INTEGER NOT NULL,
            odid INTEGER NOT NULL,
            data TEXT NOT NULL,
            mod INTEGER NOT NULL,
            usn INTEGER NOT NULL,
            flags INTEGER NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO col (crt) VALUES (0)")
    conn.executemany(
        "INSERT INTO decks (id, name) VALUES (?, ?)",
        [
            (1, "Default"),
            (2, "Target"),
            (10, "Lang"),
            (11, "Lang::Child"),
            (12, "Other"),
        ],
    )
    conn.commit()
    conn.close()

    return AnkiDirectReadStore(db_path), db_path


def _insert_card(
    db_path: Path,
    *,
    card_id: int,
    did: int = 1,
    card_type: int = 2,
    queue: int = 2,
    due: int = 0,
    ivl: int = 10,
    factor: int = 2500,
    reps: int = 0,
    lapses: int = 0,
    left: int = 0,
    odue: int = 0,
    odid: int = 0,
    data: str = "{}",
    mod: int = 1,
    usn: int = 0,
    flags: int = 0,
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO cards (
            id, did, type, queue, due, ivl, factor, reps, lapses, left,
            odue, odid, data, mod, usn, flags
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            card_id,
            did,
            card_type,
            queue,
            due,
            ivl,
            factor,
            reps,
            lapses,
            left,
            odue,
            odid,
            data,
            mod,
            usn,
            flags,
        ),
    )
    conn.commit()
    conn.close()


def _card_row(db_path: Path, card_id: int) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT
            id, did, type, queue, due, ivl, factor, reps, lapses, left,
            odue, odid, data, mod, usn, flags
        FROM cards
        WHERE id = ?
        """,
        (card_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    return {k: row[k] for k in row.keys()}


def test_move_cards_updates_existing_ids_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_000_000)

    _insert_card(db_path, card_id=10, did=1)
    _insert_card(db_path, card_id=30, did=1)

    result = store.move_cards(card_ids=[30, 10, 30, 999, 0, -1], deck="Target")

    assert result == {"moved": 2, "card_ids": [10, 30, 999], "deck": "Target"}

    row10 = _card_row(db_path, 10)
    row30 = _card_row(db_path, 30)
    assert row10["did"] == 2
    assert row30["did"] == 2
    assert row10["mod"] == 1_000_000
    assert row30["mod"] == 1_000_000
    assert row10["usn"] == -1
    assert row30["usn"] == -1


def test_move_cards_unknown_deck_raises_lookup_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    _insert_card(db_path, card_id=10, did=1)

    with pytest.raises(LookupError, match="Deck not found"):
        store.move_cards(card_ids=[10], deck="MissingDeck")

    assert _card_row(db_path, 10)["did"] == 1


def test_set_card_flag_validates_flag_range(tmp_path: Path) -> None:
    store, _db_path = _make_store(tmp_path)

    with pytest.raises(ValueError, match=r"flag must be in range 0..7"):
        store.set_card_flag(card_ids=[1], flag=-1)

    with pytest.raises(ValueError, match=r"flag must be in range 0..7"):
        store.set_card_flag(card_ids=[1], flag=8)


def test_set_card_flag_updates_existing_ids_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_000_000)

    _insert_card(db_path, card_id=1, flags=0)
    _insert_card(db_path, card_id=2, flags=4)

    result = store.set_card_flag(card_ids=[2, 1, 2, 999], flag=7)

    assert result == {"updated": 2, "card_ids": [1, 2, 999], "flag": 7}

    row1 = _card_row(db_path, 1)
    row2 = _card_row(db_path, 2)
    assert row1["flags"] == 7
    assert row2["flags"] == 7
    assert row1["mod"] == 1_000_000
    assert row2["mod"] == 1_000_000
    assert row1["usn"] == -1
    assert row2["usn"] == -1


def test_bury_then_unbury_all_restores_queue_by_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_000_000)

    _insert_card(db_path, card_id=1, card_type=0, queue=0)    # new
    _insert_card(db_path, card_id=2, card_type=2, queue=2)    # review
    _insert_card(db_path, card_id=3, card_type=3, queue=-2)   # buried relearn
    _insert_card(db_path, card_id=4, card_type=1, queue=-3)   # buried learn/sib

    buried = store.bury_cards(card_ids=[2, 1, 2, 999])
    assert buried == {"buried": 2, "card_ids": [1, 2, 999]}
    assert _card_row(db_path, 1)["queue"] == -2
    assert _card_row(db_path, 2)["queue"] == -2

    unburied = store.unbury_cards()
    assert unburied == {"unburied": 4, "scope": "all"}

    assert _card_row(db_path, 1)["queue"] == 0
    assert _card_row(db_path, 2)["queue"] == 2
    assert _card_row(db_path, 3)["queue"] == 3
    assert _card_row(db_path, 4)["queue"] == 1


def test_unbury_deck_scope_includes_children(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_000_000)

    _insert_card(db_path, card_id=101, did=10, card_type=2, queue=-2)
    _insert_card(db_path, card_id=102, did=11, card_type=0, queue=-3)
    _insert_card(db_path, card_id=103, did=12, card_type=3, queue=-2)

    result = store.unbury_cards(deck="Lang")
    assert result == {"unburied": 2, "deck": "Lang"}

    assert _card_row(db_path, 101)["queue"] == 2
    assert _card_row(db_path, 102)["queue"] == 0
    assert _card_row(db_path, 103)["queue"] == -2

    missing = store.unbury_cards(deck="Missing")
    assert missing == {"unburied": 0, "deck": "Missing"}


def test_reschedule_cards_sets_review_state_and_due(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_000_000)

    _insert_card(db_path, card_id=1, card_type=0, queue=0, due=2, ivl=0)
    _insert_card(db_path, card_id=5, card_type=1, queue=1, due=100, ivl=0)

    result = store.reschedule_cards(card_ids=[5, 1, 5, 999], days=3)
    assert result == {"rescheduled": 2, "card_ids": [1, 5, 999], "days": 3}

    expected_due = (1_000_000 // 86_400) + 3

    row1 = _card_row(db_path, 1)
    row5 = _card_row(db_path, 5)
    assert row1["type"] == 2
    assert row5["type"] == 2
    assert row1["queue"] == 2
    assert row5["queue"] == 2
    assert row1["due"] == expected_due
    assert row5["due"] == expected_due
    assert row1["ivl"] == 3
    assert row5["ivl"] == 3
    assert row1["mod"] == 1_000_000
    assert row5["mod"] == 1_000_000
    assert row1["usn"] == -1
    assert row5["usn"] == -1


def test_reschedule_cards_negative_days_raises(tmp_path: Path) -> None:
    store, _db_path = _make_store(tmp_path)

    with pytest.raises(ValueError, match="days must be >= 0"):
        store.reschedule_cards(card_ids=[1], days=-1)


def test_reset_cards_reinitializes_state_and_assigns_new_due_sequence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_000_000)

    _insert_card(
        db_path,
        card_id=1,
        card_type=0,
        queue=0,
        due=20,
        ivl=0,
        factor=0,
        reps=0,
        lapses=0,
        left=0,
        data="{}",
    )
    _insert_card(
        db_path,
        card_id=10,
        card_type=2,
        queue=2,
        due=5,
        ivl=40,
        factor=2200,
        reps=9,
        lapses=2,
        left=1002,
        odue=3,
        odid=77,
        data='{"x":1}',
    )
    _insert_card(
        db_path,
        card_id=30,
        card_type=1,
        queue=1,
        due=900,
        ivl=3,
        factor=1800,
        reps=1,
        lapses=0,
        left=2002,
        odue=0,
        odid=0,
        data='{"y":2}',
    )

    result = store.reset_cards(card_ids=[30, 10, 30, 999, 0, -5])
    assert result == {"reset": 2, "card_ids": [10, 30, 999]}

    row10 = _card_row(db_path, 10)
    row30 = _card_row(db_path, 30)
    row1 = _card_row(db_path, 1)

    assert row10["type"] == 0
    assert row30["type"] == 0
    assert row10["queue"] == 0
    assert row30["queue"] == 0
    assert row10["due"] == 21
    assert row30["due"] == 22
    assert row10["ivl"] == 0
    assert row30["ivl"] == 0
    assert row10["factor"] == 0
    assert row30["factor"] == 0
    assert row10["reps"] == 0
    assert row30["reps"] == 0
    assert row10["lapses"] == 0
    assert row30["lapses"] == 0
    assert row10["left"] == 0
    assert row30["left"] == 0
    assert row10["odue"] == 0
    assert row30["odue"] == 0
    assert row10["odid"] == 0
    assert row30["odid"] == 0
    assert row10["data"] == "{}"
    assert row30["data"] == "{}"
    assert row10["mod"] == 1_000_000
    assert row30["mod"] == 1_000_000
    assert row10["usn"] == -1
    assert row30["usn"] == -1

    # Existing new card remains unchanged.
    assert row1["queue"] == 0
    assert row1["due"] == 20


def test_card_mutator_noop_cases_return_empty_results(tmp_path: Path) -> None:
    store, _db_path = _make_store(tmp_path)

    assert store.move_cards(card_ids=[], deck="Target") == {"moved": 0, "card_ids": []}
    assert store.set_card_flag(card_ids=[], flag=1) == {"updated": 0, "card_ids": []}
    assert store.bury_cards(card_ids=[]) == {"buried": 0, "card_ids": []}
    assert store.reschedule_cards(card_ids=[], days=2) == {"rescheduled": 0, "card_ids": []}
    assert store.reset_cards(card_ids=[]) == {"reset": 0, "card_ids": []}