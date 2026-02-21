from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from anki_cli.db.anki_direct import AnkiDirectReadStore


def _make_store(tmp_path: Path, *, col_crt: int = 0) -> tuple[AnkiDirectReadStore, Path]:
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

        CREATE TABLE notetypes (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        );

        CREATE TABLE notes (
            id INTEGER PRIMARY KEY,
            guid TEXT NOT NULL,
            mid INTEGER NOT NULL,
            mod INTEGER NOT NULL,
            usn INTEGER NOT NULL,
            tags TEXT NOT NULL,
            flds TEXT NOT NULL,
            sfld TEXT NOT NULL,
            csum INTEGER NOT NULL,
            flags INTEGER NOT NULL,
            data TEXT NOT NULL
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
    conn.execute("INSERT INTO col (crt) VALUES (?)", (col_crt,))
    conn.commit()
    conn.close()

    return AnkiDirectReadStore(db_path), db_path


def _insert_deck(db_path: Path, *, did: int, name: str) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO decks (id, name) VALUES (?, ?)", (did, name))
    conn.commit()
    conn.close()


def _insert_notetype(db_path: Path, *, ntid: int, name: str) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO notetypes (id, name) VALUES (?, ?)", (ntid, name))
    conn.commit()
    conn.close()


def _insert_note(
    db_path: Path,
    *,
    note_id: int,
    guid: str,
    mid: int,
    mod: int,
    usn: int,
    tags: str,
    flds: str,
    sfld: str,
    csum: int,
    flags: int,
    data: str,
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO notes (
            id, guid, mid, mod, usn, tags, flds, sfld, csum, flags, data
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (note_id, guid, mid, mod, usn, tags, flds, sfld, csum, flags, data),
    )
    conn.commit()
    conn.close()


def _insert_card(
    db_path: Path,
    *,
    card_id: int,
    nid: int,
    did: int,
    ord_: int,
    mod: int,
    usn: int,
    card_type: int,
    queue: int,
    due: int,
    ivl: int,
    factor: int,
    reps: int,
    lapses: int,
    left: int,
    odue: int,
    odid: int,
    flags: int,
    data: str,
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO cards (
            id, nid, did, ord, mod, usn, type, queue, due, ivl, factor, reps,
            lapses, left, odue, odid, flags, data
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            card_id,
            nid,
            did,
            ord_,
            mod,
            usn,
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
            flags,
            data,
        ),
    )
    conn.commit()
    conn.close()


def _insert_revlog(
    db_path: Path,
    *,
    rid: int,
    cid: int,
    usn: int,
    ease: int,
    ivl: int,
    last_ivl: int,
    factor: int,
    duration_ms: int,
    review_type: int,
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (rid, cid, usn, ease, ivl, last_ivl, factor, duration_ms, review_type),
    )
    conn.commit()
    conn.close()


def test_get_note_returns_parsed_payload(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)

    _insert_note(
        db_path,
        note_id=100,
        guid="guid-100",
        mid=10,
        mod=123,
        usn=-1,
        tags=" alpha beta ",
        flds="Q\x1fA",
        sfld="Q",
        csum=42,
        flags=3,
        data='{"k":1}',
    )

    out = store.get_note(100)
    assert out == {
        "id": 100,
        "guid": "guid-100",
        "mid": 10,
        "mod": 123,
        "usn": -1,
        "tags": ["alpha", "beta"],
        "fields": ["Q", "A"],
        "sfld": "Q",
        "csum": 42,
        "flags": 3,
        "data": '{"k":1}',
    }


def test_get_note_missing_raises_lookup_error(tmp_path: Path) -> None:
    store, _db_path = _make_store(tmp_path)

    with pytest.raises(LookupError, match="Note not found"):
        store.get_note(999)


def test_get_card_review_payload_decodes_due_left_and_data(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path, col_crt=864000)  # day index 10

    _insert_deck(db_path, did=1, name="Default")
    _insert_notetype(db_path, ntid=10, name="Basic")
    _insert_note(
        db_path,
        note_id=100,
        guid="guid-100",
        mid=10,
        mod=111,
        usn=-1,
        tags=" alpha beta ",
        flds="Q\x1fA",
        sfld="Q",
        csum=123,
        flags=0,
        data="",
    )
    _insert_card(
        db_path,
        card_id=200,
        nid=100,
        did=1,
        ord_=0,
        mod=222,
        usn=-1,
        card_type=2,
        queue=2,
        due=5,
        ivl=15,
        factor=2500,
        reps=3,
        lapses=1,
        left=2003,
        odue=0,
        odid=0,
        flags=2,
        data='{"x":1}',
    )

    out = store.get_card(200)
    assert out == {
        "cardId": 200,
        "note": 100,
        "deckId": 1,
        "deckName": "Default",
        "ord": 0,
        "type": 2,
        "queue": 2,
        "due": 5,
        "interval": 15,
        "factor": 2500,
        "reps": 3,
        "lapses": 1,
        "left": 2003,
        "flags": 2,
        "fields": ["Q", "A"],
        "tags": ["alpha", "beta"],
        "data": '{"x":1}',
        "notetype_id": 10,
        "notetype_name": "Basic",
        "due_info": {
            "kind": "review_day_index",
            "raw": 5,
            "day_index": 5,
            "epoch_secs": (10 + 5) * 86400,
        },
        "left_info": {
            "raw": 2003,
            "today_remaining": 2,
            "until_graduation": 3,
        },
        "data_parsed": {"x": 1},
    }


def test_get_card_unknown_type_and_missing_optional_joins(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)

    # mid and did intentionally do not exist in notetypes/decks.
    _insert_note(
        db_path,
        note_id=101,
        guid="guid-101",
        mid=999,
        mod=10,
        usn=0,
        tags=" x ",
        flds="Only",
        sfld="Only",
        csum=1,
        flags=0,
        data="",
    )
    _insert_card(
        db_path,
        card_id=201,
        nid=101,
        did=42,
        ord_=1,
        mod=22,
        usn=0,
        card_type=99,
        queue=7,
        due=123,
        ivl=0,
        factor=0,
        reps=0,
        lapses=0,
        left=-1,
        odue=0,
        odid=0,
        flags=0,
        data="{not json",
    )

    out = store.get_card(201)
    assert out["deckName"] == ""
    assert out["notetype_id"] == 999
    assert out["notetype_name"] == ""
    assert out["due_info"] == {"kind": "raw", "raw": 123, "queue": 7, "type": 99}
    assert out["left_info"] == {"raw": -1}
    assert out["data_parsed"] == "{not json"


def test_get_card_missing_raises_lookup_error(tmp_path: Path) -> None:
    store, _db_path = _make_store(tmp_path)

    with pytest.raises(LookupError, match="Card not found"):
        store.get_card(999)


def test_get_revlog_returns_descending_order_and_decoded_fields(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)

    _insert_revlog(
        db_path,
        rid=1700000000001,
        cid=200,
        usn=-1,
        ease=3,
        ivl=-60,
        last_ivl=10,
        factor=500,   # fsrs_difficulty
        duration_ms=111,
        review_type=4,  # manual
    )
    _insert_revlog(
        db_path,
        rid=1700000000002,
        cid=200,
        usn=-1,
        ease=4,
        ivl=20,
        last_ivl=-30,
        factor=2500,  # sm2 ease
        duration_ms=222,
        review_type=1,  # review
    )
    _insert_revlog(
        db_path,
        rid=1700000000003,
        cid=200,
        usn=0,
        ease=1,
        ivl=0,
        last_ivl=0,
        factor=0,  # unknown
        duration_ms=333,
        review_type=2,  # relearn
    )

    out = store.get_revlog(200, limit=2)
    assert [item["id"] for item in out] == [1700000000003, 1700000000002]

    first = out[0]
    assert first["card_id"] == 200
    assert first["review_type_name"] == "relearn"
    assert first["interval"] == {"raw": 0, "unit": "days", "days": 0, "seconds": None}
    assert first["factor_info"] == {
        "raw": 0,
        "model": "unknown",
        "difficulty": None,
        "ease_multiplier": None,
    }

    second = out[1]
    assert second["review_type_name"] == "review"
    assert second["interval"] == {"raw": 20, "unit": "days", "days": 20, "seconds": None}
    assert second["last_interval"] == {"raw": -30, "unit": "seconds", "seconds": 30, "days": None}
    assert second["factor_info"] == {
        "raw": 2500,
        "model": "sm2_ease_permille",
        "difficulty": None,
        "ease_multiplier": 2.5,
    }


def test_get_revlog_limit_is_bounded_to_at_least_one(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)

    _insert_revlog(
        db_path,
        rid=1,
        cid=7,
        usn=0,
        ease=3,
        ivl=1,
        last_ivl=0,
        factor=2500,
        duration_ms=10,
        review_type=1,
    )
    _insert_revlog(
        db_path,
        rid=2,
        cid=7,
        usn=0,
        ease=4,
        ivl=2,
        last_ivl=1,
        factor=2500,
        duration_ms=20,
        review_type=1,
    )

    out = store.get_revlog(7, limit=0)
    assert len(out) == 1
    assert out[0]["id"] == 2


def test_get_revlog_large_limit_is_capped_but_returns_available_rows(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)

    for rid in (10, 11, 12):
        _insert_revlog(
            db_path,
            rid=rid,
            cid=9,
            usn=0,
            ease=3,
            ivl=1,
            last_ivl=0,
            factor=2500,
            duration_ms=5,
            review_type=1,
        )

    out = store.get_revlog(9, limit=5000)
    assert len(out) == 3
    assert [item["id"] for item in out] == [12, 11, 10]


def test_get_revlog_empty_returns_empty_list(tmp_path: Path) -> None:
    store, _db_path = _make_store(tmp_path)

    assert store.get_revlog(123456) == []