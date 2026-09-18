"""Regression tests for #57: empty (re)learning steps must reach py-fsrs as-is.

Blanked steps in deck options are a real configuration, not a missing one
(Anki's preset editor allows it, and FSRS setups often drop relearning steps):
a review card rated Again stays in Review with the FSRS-computed interval
instead of entering a phantom 10-minute relearn step, and a new card
graduates to Review on its first answer. Only a *missing* deck_config row
falls back to the library defaults.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import anki_cli.db.scheduling as scheduling_mod
from anki_cli.db.store import AnkiDirectStore
from anki_cli.proto.anki.deck_config import DeckConfigConfig
from tests.anki_schema import connect
from tests.conftest import Collection, new_collection, normal_deck_kind

NOW_SEC = 1_700_000_000
# crt is 0 and col.conf is empty, so timing is v1: today is a plain day index.
TODAY = NOW_SEC // 86400


def _make_store(tmp_path: Path) -> tuple[AnkiDirectStore, Path]:
    col = new_collection(tmp_path / "collection.anki2", seed=False)
    col.insert_notetype(id=10, name="Basic", fields=["Front", "Back"])
    col.insert_note(id=1000, fields=["Q", "A"])
    return col.store(writable=False), col.db_path


def _insert_deck(db_path: Path, *, did: int = 1, config_id: int = 1) -> None:
    Collection(db_path).insert_deck(
        id=did, name="Default", kind=normal_deck_kind(config_id=config_id)
    )


def _insert_deck_config(
    db_path: Path,
    *,
    config_id: int = 1,
    learn_steps: list[float],
    relearn_steps: list[float],
) -> None:
    # Exactly the preset these tests always used: retention + steps, nothing else
    # (so the builder's other defaults do not creep into the scheduling under test).
    cfg = DeckConfigConfig(
        desired_retention=0.9,
        learn_steps=learn_steps,
        relearn_steps=relearn_steps,
    )
    Collection(db_path).insert_deck_config(id=config_id, name="Default", config=bytes(cfg))


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
    Collection(db_path).insert_card(
        id=card_id, nid=1000, did=1, mod=mod, type=card_type, queue=queue, due=due, ivl=ivl,
        factor=factor, reps=1, left=left,
    )


def _card_row(db_path: Path, card_id: int) -> dict[str, Any]:
    conn = connect(str(db_path))
    row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    conn.close()
    assert row is not None
    return dict(row)


def _pin_clock(monkeypatch: pytest.MonkeyPatch, store: AnkiDirectStore) -> None:
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    ids = iter(range(9001, 9100))
    monkeypatch.setattr(
        store, "_allocate_epoch_ms_id", lambda conn, table: next(ids)
    )
    monkeypatch.setattr(time, "time", lambda: NOW_SEC)

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return datetime.fromtimestamp(NOW_SEC, tz=UTC)

    monkeypatch.setattr(scheduling_mod, "datetime", _Now)


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
