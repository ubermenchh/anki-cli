from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
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

        CREATE TABLE cards (
            id INTEGER PRIMARY KEY,
            nid INTEGER NOT NULL,
            did INTEGER NOT NULL,
            ord INTEGER NOT NULL,
            mod INTEGER NOT NULL,
            usn INTEGER NOT NULL,
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
            flags INTEGER NOT NULL,
            data TEXT NOT NULL
        );

        CREATE TABLE revlog (
            id INTEGER PRIMARY KEY,
            cid INTEGER NOT NULL,
            usn INTEGER NOT NULL,
            ease INTEGER NOT NULL,
            ivl INTEGER NOT NULL,
            lastIvl INTEGER NOT NULL,
            factor INTEGER NOT NULL,
            time INTEGER NOT NULL,
            type INTEGER NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO col (crt) VALUES (0)")
    conn.execute(
        """
        INSERT INTO cards (
            id, nid, did, ord, mod, usn, type, queue, due, ivl, factor, reps, lapses,
            left, odue, odid, flags, data
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            100,   # id
            1000,  # nid
            1,     # did
            0,     # ord
            111,   # mod
            0,     # usn
            2,     # type
            2,     # queue
            30,    # due
            10,    # ivl
            2500,  # factor
            20,    # reps
            1,     # lapses
            0,     # left
            0,     # odue
            0,     # odid
            3,     # flags
            "{}",  # data
        ),
    )
    conn.commit()
    conn.close()

    return AnkiDirectReadStore(db_path), db_path


def _card_row(db_path: Path, card_id: int) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT id, did, ord, type, queue, due, ivl, factor,
               reps, lapses, left, flags, data, mod, usn
        FROM cards
        WHERE id = ?
        """,
        (card_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    return {k: row[k] for k in row.keys()}


def _revlog_rows(db_path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, cid, usn, ease, ivl, lastIvl, factor, time, type FROM revlog ORDER BY id"
    ).fetchall()
    conn.close()
    return [{k: row[k] for k in row.keys()} for row in rows]


def test_answer_card_invalid_ease_raises_value_error(tmp_path: Path) -> None:
    store, _db_path = _make_store(tmp_path)

    with pytest.raises(ValueError, match="ease must be one of"):
        store.answer_card(100, ease=9)


def test_answer_card_missing_card_raises_lookup_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, _db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    with pytest.raises(LookupError, match="Card not found"):
        store.answer_card(999, ease=3)


def test_answer_card_updates_card_and_writes_revlog_non_lapse(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)

    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(store, "_allocate_epoch_ms_id", lambda conn, table: 9001)

    # Keep flow deterministic and independent of FSRS internals.
    monkeypatch.setattr(
        store,
        "_build_scheduler",
        lambda conn, deck_id: (
            type(
                "FakeScheduler",
                (),
                {
                    "review_card": lambda self, card, rating, review_datetime: (
                        SimpleNamespace(stability=3.2, difficulty=6.7),
                        None,
                    )
                },
            )(),
            0.9,
            2,
            1,
        ),
    )
    monkeypatch.setattr(
        store,
        "_card_row_to_fsrs",
        lambda row, *, col_crt_sec, now_dt: SimpleNamespace(
            state=direct_mod.State.Learning,
            step=0,
            stability=None,
            difficulty=None,
            last_review=None,
        ),
    )
    monkeypatch.setattr(
        store,
        "_map_fsrs_result_to_anki",
        lambda **kwargs: (2, 2, 33, 44, 0, 123456),
    )

    result = store.answer_card(100, ease=3)

    assert result == {
        "card_id": 100,
        "ease": 3,
        "answered": True,
        "queue": 2,
        "type": 2,
        "due": 33,
        "interval": 44,
    }

    row = _card_row(db_path, 100)
    assert row["type"] == 2
    assert row["queue"] == 2
    assert row["due"] == 33
    assert row["ivl"] == 44
    assert row["reps"] == 21
    assert row["lapses"] == 1  # ease != 1
    assert row["left"] == 0
    assert row["usn"] == -1
    assert row["mod"] > 0

    data = json.loads(row["data"])
    assert data["pos"] == 0
    assert data["dr"] == 0.9
    assert data["s"] == 3.2
    assert data["d"] == 6.7
    assert data["lrt"] == row["mod"]

    revlog = _revlog_rows(db_path)
    assert revlog == [
        {
            "id": 9001,
            "cid": 100,
            "usn": -1,
            "ease": 3,
            "ivl": 44,
            "lastIvl": 10,
            "factor": 670,  # round(6.7 * 100)
            "time": 0,
            "type": 1,  # review
        }
    ]


def test_answer_card_lapse_increments_lapses_and_sets_relearn_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)

    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(store, "_allocate_epoch_ms_id", lambda conn, table: 9002)

    monkeypatch.setattr(
        store,
        "_build_scheduler",
        lambda conn, deck_id: (
            type(
                "FakeScheduler",
                (),
                {
                    "review_card": lambda self, card, rating, review_datetime: (
                        SimpleNamespace(stability=4.0, difficulty=5.5),
                        None,
                    )
                },
            )(),
            0.9,
            2,
            1,
        ),
    )
    monkeypatch.setattr(
        store,
        "_card_row_to_fsrs",
        lambda row, *, col_crt_sec, now_dt: SimpleNamespace(
            state=direct_mod.State.Learning,
            step=0,
            stability=None,
            difficulty=None,
            last_review=None,
        ),
    )
    monkeypatch.setattr(
        store,
        "_map_fsrs_result_to_anki",
        lambda **kwargs: (3, 2, 50, 60, 0, 123500),
    )

    result = store.answer_card(100, ease=1)

    assert result["answered"] is True
    assert result["type"] == 3
    assert result["queue"] == 2
    assert result["due"] == 50
    assert result["interval"] == 60

    row = _card_row(db_path, 100)
    assert row["reps"] == 21
    assert row["lapses"] == 2  # incremented when ease == 1

    revlog = _revlog_rows(db_path)
    assert revlog == [
        {
            "id": 9002,
            "cid": 100,
            "usn": -1,
            "ease": 1,
            "ivl": 60,
            "lastIvl": 10,
            "factor": 550,
            "time": 0,
            "type": 2,  # relearn
        }
    ]