"""Regression tests for #19 / #43: day-learn (queue 3) stores a day index, not an epoch.

Anki keeps two units in ``cards.due`` for learning cards: intraday learning
(queue 1) stores a unix epoch, day-learn (queue 3) stores a day index relative to
``col.crt``. The two are told apart by ``LEARN_DUE_EPOCH_THRESHOLD``.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fsrs import Card as FSRSCard
from fsrs import State

import anki_cli.db.anki_direct as direct_mod
from anki_cli.db.anki_direct import (
    LEARN_DUE_EPOCH_THRESHOLD,
    AnkiDirectReadStore,
    is_intraday_learn_due,
    queue_from_type_sql,
)
from tests.integration.conftest import COL_TABLE_SQL, insert_col_row

CRT = 1_700_000_000  # 2023-11-14T22:13:20Z
CRT_DAY = CRT // 86400


def _make_store(tmp_path: Path) -> tuple[AnkiDirectReadStore, Path]:
    db_path = tmp_path / "collection.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE decks (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE cards (
            id INTEGER PRIMARY KEY,
            did INTEGER NOT NULL DEFAULT 1,
            type INTEGER NOT NULL,
            queue INTEGER NOT NULL,
            due INTEGER NOT NULL,
            left INTEGER NOT NULL DEFAULT 0,
            data TEXT NOT NULL DEFAULT '',
            mod INTEGER NOT NULL DEFAULT 0,
            usn INTEGER NOT NULL DEFAULT 0
        );
        """)
    conn.executescript(COL_TABLE_SQL)
    insert_col_row(conn, crt=CRT)
    conn.execute("INSERT INTO decks (id, name) VALUES (1, 'Default')")
    conn.commit()
    conn.close()
    return AnkiDirectReadStore(db_path), db_path


def _insert_card(db_path: Path, *, card_id: int, type_: int, queue: int, due: int) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO cards (id, type, queue, due) VALUES (?, ?, ?, ?)",
        (card_id, type_, queue, due),
    )
    conn.commit()
    conn.close()


def _card_row(db_path: Path, card_id: int) -> sqlite3.Row:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    conn.close()
    assert row is not None
    return row


# --- the discriminator ------------------------------------------------------


@pytest.mark.parametrize(
    ("due", "expected"),
    [
        (0, False),
        (20_050, False),  # plausible day index (2024-ish)
        (LEARN_DUE_EPOCH_THRESHOLD, False),
        (LEARN_DUE_EPOCH_THRESHOLD + 1, True),
        (1_700_000_600, True),
    ],
)
def test_is_intraday_learn_due_threshold(due: int, expected: bool) -> None:
    assert is_intraday_learn_due(due) is expected


def test_queue_from_type_sql_matches_rslib_restore_queue_from_type(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    sql = f"SELECT {queue_from_type_sql()} FROM (SELECT ? AS type, ? AS due)"
    cases = [
        (0, 5, 0),  # new -> new
        (2, 19_800, 2),  # review -> review
        (1, 1_700_000_300, 1),  # learn, epoch due -> intraday
        (1, 20_050, 3),  # learn, day index -> day-learn
        (3, 1_700_000_300, 1),  # relearn, epoch due -> intraday
        (3, 20_050, 3),  # relearn, day index -> day-learn
    ]
    for type_, due, expected in cases:
        assert conn.execute(sql, (type_, due)).fetchone()[0] == expected, (type_, due)


# --- decode ------------------------------------------------------------------


def test_decode_due_distinguishes_intraday_and_day_learn(tmp_path: Path) -> None:
    store, _ = _make_store(tmp_path)

    assert store._decode_due(card_type=1, queue=1, due_raw=1_700_000_600, col_crt_sec=CRT) == {
        "kind": "learn_epoch_secs",
        "raw": 1_700_000_600,
        "epoch_secs": 1_700_000_600,
    }
    assert store._decode_due(card_type=1, queue=3, due_raw=CRT_DAY + 30, col_crt_sec=CRT) == {
        "kind": "learn_day_index",
        "raw": CRT_DAY + 30,
        "day_index": CRT_DAY + 30,
        "epoch_secs": (CRT_DAY + CRT_DAY + 30) * 86400,
    }
    # A suspended relearning card keeps its day-index due; the unit is decided
    # by the value, not the (negative) queue.
    out = store._decode_due(card_type=3, queue=-1, due_raw=12, col_crt_sec=None)
    assert out == {"kind": "learn_day_index", "raw": 12, "day_index": 12}


# --- FSRS mapping ------------------------------------------------------------


def test_card_row_to_fsrs_day_learn_uses_day_index(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)
    # type 1 (learning), queue 3, due = 30 days after crt day.
    _insert_card(db_path, card_id=1, type_=1, queue=3, due=30)
    row = _card_row(db_path, 1)

    card = store._card_row_to_fsrs(row, col_crt_sec=CRT, now_dt=datetime.now(UTC))

    assert card.due == datetime.fromtimestamp((CRT_DAY + 30) * 86400, tz=UTC)
    assert card.due.year >= 2023, "a day index must not be read as a 1970 epoch"
    # Day-learn is a queue, not relearning; relearning is type 3.
    assert card.state == State.Learning


def test_card_row_to_fsrs_relearning_is_a_property_of_type(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)
    _insert_card(db_path, card_id=1, type_=3, queue=1, due=1_700_000_600)
    row = _card_row(db_path, 1)

    card = store._card_row_to_fsrs(row, col_crt_sec=CRT, now_dt=datetime.now(UTC))

    assert card.due == datetime.fromtimestamp(1_700_000_600, tz=UTC)
    assert card.state == State.Relearning


def test_map_fsrs_result_short_step_stays_intraday(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store, db_path = _make_store(tmp_path)
    _insert_card(db_path, card_id=1, type_=0, queue=0, due=0)
    row = _card_row(db_path, 1)
    now = datetime.fromtimestamp(CRT + 100 * 86400, tz=UTC)

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return now

    monkeypatch.setattr(direct_mod, "datetime", _Now)
    next_card = FSRSCard(card_id=1, state=State.Learning, step=1, due=now.replace(minute=10))
    type_, queue, due, _ivl, _left, next_due_epoch = store._map_fsrs_result_to_anki(
        current_row=row,
        next_card=next_card,
        col_crt_sec=CRT,
        learn_step_count=3,
        relearn_step_count=1,
    )

    assert (type_, queue) == (1, 1)
    assert due == next_due_epoch == int(next_card.due.timestamp())
    assert is_intraday_learn_due(due)


def test_map_fsrs_result_long_step_moves_to_day_learn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A learning step of >= 1 day becomes queue 3 with a day-index due (Anki v3)."""
    store, db_path = _make_store(tmp_path)
    _insert_card(db_path, card_id=1, type_=2, queue=2, due=0)
    row = _card_row(db_path, 1)
    now = datetime.fromtimestamp(CRT + 100 * 86400, tz=UTC)

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return now

    monkeypatch.setattr(direct_mod, "datetime", _Now)
    next_due = datetime.fromtimestamp(now.timestamp() + 3 * 86400, tz=UTC)
    next_card = FSRSCard(card_id=1, state=State.Relearning, step=0, due=next_due)
    type_, queue, due, _ivl, _left, next_due_epoch = store._map_fsrs_result_to_anki(
        current_row=row,
        next_card=next_card,
        col_crt_sec=CRT,
        learn_step_count=3,
        relearn_step_count=2,
    )

    assert (type_, queue) == (3, 3)
    assert due == CRT_DAY + 100 + 3 - CRT_DAY  # day index relative to crt
    assert not is_intraday_learn_due(due)
    assert next_due_epoch == int(next_due.timestamp())
    # Round-trips through the restore rule back to queue 3.
    conn = sqlite3.connect(":memory:")
    sql = f"SELECT {queue_from_type_sql()} FROM (SELECT ? AS type, ? AS due)"
    assert conn.execute(sql, (type_, due)).fetchone()[0] == 3


# --- next due for deck --------------------------------------------------------


def test_get_next_due_for_deck_orders_by_absolute_time_across_units(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)
    # Review due 5 days after crt, intraday learn due at crt + 10 days,
    # day-learn due 2 days after crt -> day-learn is earliest.
    _insert_card(db_path, card_id=1, type_=2, queue=2, due=5)
    _insert_card(db_path, card_id=2, type_=1, queue=1, due=CRT + 10 * 86400)
    _insert_card(db_path, card_id=3, type_=1, queue=3, due=2)

    out = store._get_next_due_for_deck("Default")

    assert out == {"queue": 3, "day_index": 2, "epoch_secs": (CRT_DAY + 2) * 86400}


def test_get_next_due_for_deck_review_does_not_beat_earlier_learn(tmp_path: Path) -> None:
    """Pre-fix, `due * 86400` compared a day index against an epoch and lost."""
    store, db_path = _make_store(tmp_path)
    _insert_card(db_path, card_id=1, type_=2, queue=2, due=200)  # ~6 months after crt
    _insert_card(db_path, card_id=2, type_=1, queue=1, due=CRT + 60)  # one minute after crt

    out = store._get_next_due_for_deck("Default")

    assert out == {"queue": 1, "epoch_secs": CRT + 60}
