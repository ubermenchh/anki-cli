"""Regression tests for #57: empty (re)learning steps must reach py-fsrs as-is.

Blanked steps in deck options are a real configuration, not a missing one
(Anki's preset editor allows it, and FSRS setups often drop relearning steps):
a review card rated Again stays in Review with the FSRS-computed interval
instead of entering a phantom 10-minute relearn step, and a new card
graduates to Review on its first answer. Only a *missing* deck_config row
falls back to the library defaults.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import anki_cli.db.anki_direct as direct_mod
from anki_cli.db.anki_direct import AnkiDirectReadStore
from anki_cli.proto.anki.deck_config import DeckConfigConfig
from anki_cli.proto.anki.decks import DeckKindContainer, DeckNormal
from tests.integration.conftest import COL_TABLE_SQL, insert_col_row

NOW_SEC = 1_700_000_000
# crt is 0 and col.conf is empty, so timing is v1: today is a plain day index.
TODAY = NOW_SEC // 86400


def _make_store(tmp_path: Path) -> tuple[AnkiDirectReadStore, Path]:
    db_path = tmp_path / "collection.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
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

        CREATE TABLE decks (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            kind BLOB NOT NULL
        );

        CREATE TABLE deck_config (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            config BLOB NOT NULL
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
        """)
    conn.executescript(COL_TABLE_SQL)
    insert_col_row(conn, crt=0)
    conn.commit()
    conn.close()

    return AnkiDirectReadStore(db_path), db_path


def _insert_deck(db_path: Path, *, did: int = 1, config_id: int = 1) -> None:
    kind = DeckKindContainer(normal=DeckNormal(config_id=config_id))
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO decks (id, name, kind) VALUES (?, ?, ?)",
        (did, "Default", bytes(kind)),
    )
    conn.commit()
    conn.close()


def _insert_deck_config(
    db_path: Path,
    *,
    config_id: int = 1,
    learn_steps: list[float],
    relearn_steps: list[float],
) -> None:
    cfg = DeckConfigConfig(
        desired_retention=0.9,
        learn_steps=learn_steps,
        relearn_steps=relearn_steps,
    )
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO deck_config (id, name, config) VALUES (?, ?, ?)",
        (config_id, "Default", bytes(cfg)),
    )
    conn.commit()
    conn.close()


def _insert_card(
    db_path: Path,
    *,
    card_id: int,
    card_type: int,
    queue: int,
    due: int,
    ivl: int = 0,
    factor: int = 0,
    mod: int = 111,
    left: int = 0,
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
            1000,  # nid
            1,  # did
            0,  # ord
            mod,
            0,  # usn
            card_type,
            queue,
            due,
            ivl,
            factor,
            1,  # reps
            0,  # lapses
            left,
            0,  # odue
            0,  # odid
            0,  # flags
            "{}",
        ),
    )
    conn.commit()
    conn.close()


def _card_row(db_path: Path, card_id: int) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    conn.close()
    assert row is not None
    return dict(row)


def _pin_clock(monkeypatch: pytest.MonkeyPatch, store: AnkiDirectReadStore) -> None:
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    ids = iter(range(9001, 9100))
    monkeypatch.setattr(
        store, "_allocate_epoch_ms_id", lambda conn, table: next(ids)
    )
    monkeypatch.setattr(direct_mod.time, "time", lambda: NOW_SEC)

    class _Now(direct_mod.datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return direct_mod.datetime.fromtimestamp(NOW_SEC, tz=direct_mod.UTC)

    monkeypatch.setattr(direct_mod, "datetime", _Now)


def test_empty_step_lists_reach_the_scheduler_untouched(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)
    _insert_deck(db_path)
    _insert_deck_config(db_path, learn_steps=[], relearn_steps=[])

    with store._connect() as conn:
        scheduler, _dr, learn_count, relearn_count = store._build_scheduler(conn, 1)

    assert scheduler.learning_steps == ()
    assert scheduler.relearning_steps == ()
    assert (learn_count, relearn_count) == (0, 0)


def test_missing_deck_config_row_falls_back_to_fsrs_defaults(tmp_path: Path) -> None:
    """Only when there is no deck_config row at all do the built-in defaults apply."""
    store, db_path = _make_store(tmp_path)
    _insert_deck(db_path, config_id=1)

    with store._connect() as conn:
        scheduler, _dr, learn_count, relearn_count = store._build_scheduler(conn, 1)

    assert scheduler.learning_steps == (timedelta(minutes=1), timedelta(minutes=10))
    assert scheduler.relearning_steps == (timedelta(minutes=10),)
    assert (learn_count, relearn_count) == (2, 1)


def test_empty_relearn_steps_keep_review_card_in_review_on_again(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """#57's prescribed test: relearn_steps=[] + Again -> type 2, queue 2."""
    store, db_path = _make_store(tmp_path)
    _insert_deck(db_path)
    _insert_deck_config(db_path, learn_steps=[1.0, 10.0], relearn_steps=[])
    _insert_card(
        db_path, card_id=100, card_type=2, queue=2, due=TODAY, ivl=10, factor=2500
    )
    _pin_clock(monkeypatch, store)

    result = store.answer_card(100, ease=1)

    assert result["type"] == 2
    assert result["queue"] == 2
    row = _card_row(db_path, 100)
    assert (row["type"], row["queue"]) == (2, 2)
    assert row["left"] == 0
    # The interval is FSRS-computed (and fuzzed); whatever it lands on, the due
    # is a review day index today + ivl, not a 10-minute intraday step.
    assert row["ivl"] >= 1
    assert row["due"] == TODAY + row["ivl"]
    assert row["lapses"] == 1


@pytest.mark.parametrize("ease", [1, 3])
def test_empty_learn_steps_graduate_new_card_on_first_answer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ease: int,
) -> None:
    """With no learning steps the first answer graduates to Review, like Anki."""
    store, db_path = _make_store(tmp_path)
    _insert_deck(db_path)
    _insert_deck_config(db_path, learn_steps=[], relearn_steps=[10.0])
    _insert_card(db_path, card_id=200, card_type=0, queue=0, due=7)
    _pin_clock(monkeypatch, store)

    result = store.answer_card(200, ease=ease)

    assert result["type"] == 2
    assert result["queue"] == 2
    row = _card_row(db_path, 200)
    assert (row["type"], row["queue"]) == (2, 2)
    assert row["left"] == 0
    assert row["ivl"] >= 1
    assert row["due"] == TODAY + row["ivl"]


def test_populated_steps_still_drive_learning_and_relearning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Control: explicit steps keep the classic intraday-learning behavior."""
    store, db_path = _make_store(tmp_path)
    _insert_deck(db_path)
    _insert_deck_config(db_path, learn_steps=[1.0, 10.0], relearn_steps=[10.0])
    _insert_card(
        db_path, card_id=100, card_type=2, queue=2, due=TODAY, ivl=10, factor=2500
    )
    _insert_card(db_path, card_id=200, card_type=0, queue=0, due=7)
    _pin_clock(monkeypatch, store)

    # Lapse enters relearning step 0: type 3, intraday queue, due in 10 minutes.
    result = store.answer_card(100, ease=1)
    assert (result["type"], result["queue"]) == (3, 1)
    row = _card_row(db_path, 100)
    assert (row["type"], row["queue"]) == (3, 1)
    assert row["due"] == NOW_SEC + 600
    assert row["left"] == 1001

    # Good on a new card advances to the second learning step, still intraday.
    result = store.answer_card(200, ease=3)
    assert (result["type"], result["queue"]) == (1, 1)
    row = _card_row(db_path, 200)
    assert (row["type"], row["queue"]) == (1, 1)
    assert row["due"] == NOW_SEC + 600
    assert row["left"] == 1001
