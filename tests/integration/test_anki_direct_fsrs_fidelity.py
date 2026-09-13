"""Regression tests for #22 items 1-5: FSRS scheduler fidelity with Anki.

1. learning step recovered from cards.left instead of reset to 0
2. FSRS-4.5 / FSRS-5 weights upgraded to FSRS-6 the way fsrs-rs does
3. interval fuzz seeded per card + reps so preview == answer
4. revlog.type from the card's state before the answer (incl. early review)
5. last review time comes from the revlog, not a non-Anki data key
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fsrs import Rating, Scheduler, State

import anki_cli.db.anki_direct as direct_mod
from anki_cli.db.anki_direct import (
    FSRS6_DEFAULT_PARAMETERS,
    REVLOG_KIND_FILTERED,
    REVLOG_KIND_LEARNING,
    REVLOG_KIND_RELEARNING,
    REVLOG_KIND_REVIEW,
    AnkiDirectReadStore,
    clamp_fsrs_parameters,
    fsrs_fuzz_seed,
    upgrade_fsrs_parameters,
)
from anki_cli.db.timing import sched_timing_today_v1
from anki_cli.proto.anki.deck_config import DeckConfigConfig
from anki_cli.proto.anki.decks import DeckKindContainer, DeckNormal
from tests.integration.conftest import COL_TABLE_SQL, insert_col_row

CRT = 1_700_000_000
NOW = CRT + 100 * 86400 + 3600  # one hour into scheduling day 100 (v1 timing)


def _make_store(tmp_path: Path, *, deck_config: DeckConfigConfig | None = None):
    db_path = tmp_path / "collection.anki2"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE decks (id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind BLOB NOT NULL);
        CREATE TABLE deck_config (id INTEGER PRIMARY KEY, name TEXT NOT NULL, config BLOB NOT NULL);
        CREATE TABLE cards (
            id INTEGER PRIMARY KEY, nid INTEGER NOT NULL DEFAULT 1, did INTEGER NOT NULL DEFAULT 1,
            ord INTEGER NOT NULL DEFAULT 0, mod INTEGER NOT NULL DEFAULT 0,
            usn INTEGER NOT NULL DEFAULT 0, type INTEGER NOT NULL, queue INTEGER NOT NULL,
            due INTEGER NOT NULL, ivl INTEGER NOT NULL DEFAULT 0,
            factor INTEGER NOT NULL DEFAULT 0, reps INTEGER NOT NULL DEFAULT 0,
            lapses INTEGER NOT NULL DEFAULT 0, left INTEGER NOT NULL DEFAULT 0,
            odue INTEGER NOT NULL DEFAULT 0, odid INTEGER NOT NULL DEFAULT 0,
            flags INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE revlog (
            id INTEGER PRIMARY KEY, cid INTEGER NOT NULL, usn INTEGER NOT NULL,
            ease INTEGER NOT NULL, ivl INTEGER NOT NULL, lastIvl INTEGER NOT NULL,
            factor INTEGER NOT NULL, time INTEGER NOT NULL, type INTEGER NOT NULL
        );
        """)
    conn.executescript(COL_TABLE_SQL)
    insert_col_row(conn, crt=CRT)
    conn.execute(
        "INSERT INTO decks VALUES (1, 'Default', ?)",
        (bytes(DeckKindContainer(normal=DeckNormal(config_id=1))),),
    )
    cfg = deck_config or DeckConfigConfig(
        learn_steps=[1.0, 10.0, 60.0], relearn_steps=[10.0, 30.0], desired_retention=0.9
    )
    conn.execute("INSERT INTO deck_config VALUES (1, 'Default', ?)", (bytes(cfg),))
    conn.commit()
    conn.close()
    store = AnkiDirectReadStore(db_path)
    store._ensure_write_safe = lambda: None  # type: ignore[method-assign]
    return store, db_path


def _insert_card(db_path: Path, **cols: object) -> None:
    conn = sqlite3.connect(str(db_path))
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT INTO cards ({keys}) VALUES ({marks})", tuple(cols.values()))
    conn.commit()
    conn.close()


def _insert_revlog(db_path: Path, *, rid: int, cid: int, ease: int, type_: int) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO revlog VALUES (?, ?, 0, ?, 1, 0, 2500, 0, ?)", (rid, cid, ease, type_)
    )
    conn.commit()
    conn.close()


def _row(db_path: Path, cid: int) -> sqlite3.Row:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM cards WHERE id = ?", (cid,)).fetchone()
    conn.close()
    assert row is not None
    return row


def _revlog(db_path: Path, cid: int) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM revlog WHERE cid = ? ORDER BY id", (cid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    now = datetime.fromtimestamp(NOW, tz=UTC)

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return now

    monkeypatch.setattr(direct_mod, "datetime", _Now)
    monkeypatch.setattr(direct_mod.time, "time", lambda: NOW)
    return now


# --- 1. learning step from `left` ------------------------------------------------


def test_card_row_to_fsrs_recovers_step_from_left(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)
    timing = sched_timing_today_v1(CRT, NOW)
    now = datetime.fromtimestamp(NOW, tz=UTC)
    # Three learn steps, one remaining (left = 1*1000 + 1) -> FSRS step index 2.
    _insert_card(db_path, id=1, type=1, queue=1, due=NOW + 60, left=1001)
    # Two relearn steps, both remaining -> step 0.
    _insert_card(db_path, id=2, type=3, queue=1, due=NOW + 60, left=2002)
    # New card: left = 0 -> we pass None and py-fsrs normalizes a fresh Learning
    # card to step 0 in Card.__init__.
    _insert_card(db_path, id=3, type=0, queue=0, due=5, left=0)

    def to_fsrs(cid: int):
        return store._card_row_to_fsrs(
            _row(db_path, cid),
            timing=timing,
            now_dt=now,
            learn_step_count=3,
            relearn_step_count=2,
        )

    assert to_fsrs(1).step == 2
    assert to_fsrs(2).step == 0
    assert to_fsrs(3).step == 0


def test_answer_last_learning_step_graduates_instead_of_restarting(
    clock: datetime, tmp_path: Path
) -> None:
    """Issue repro: steps [1, 10, 60] min, one step left, rate Good -> Review.
    Pre-fix the step was reset to 0 and the card got two extra learning steps."""
    store, db_path = _make_store(tmp_path)
    _insert_card(db_path, id=1, type=1, queue=1, due=NOW - 30, left=1001, reps=2)
    _insert_revlog(db_path, rid=(NOW - 3600) * 1000, cid=1, ease=3, type_=0)
    _insert_revlog(db_path, rid=(NOW - 600) * 1000, cid=1, ease=3, type_=0)

    result = store.answer_card(1, ease=3)

    assert (result["type"], result["queue"]) == (2, 2)
    assert _row(db_path, 1)["left"] == 0


# --- 2. FSRS parameter upgrades ---------------------------------------------------


def test_upgrade_fsrs_parameters_matches_fsrs_rs_transforms() -> None:
    six = list(FSRS6_DEFAULT_PARAMETERS)
    assert upgrade_fsrs_parameters(six) == (six, "fsrs6")

    five = [float(i) for i in range(1, 20)]
    assert upgrade_fsrs_parameters(five) == ([*five, 0.0, 0.5], "fsrs5-upgraded")

    four = [1.0] * 17
    four[4], four[5], four[6] = 6.0, 0.5, 1.0
    upgraded, label = upgrade_fsrs_parameters(four)
    assert label == "fsrs4.5-upgraded"
    assert len(upgraded) == 21
    assert upgraded[4] == pytest.approx(0.5 * 2.0 + 6.0)
    assert upgraded[5] == pytest.approx(math.log(0.5 * 3.0 + 1.0) / 3.0)
    assert upgraded[6] == pytest.approx(1.5)
    assert upgraded[17:] == [0.0, 0.0, 0.0, 0.5]

    assert upgrade_fsrs_parameters([]) == (six, "default")
    assert upgrade_fsrs_parameters([0.1] * 23)[1] == "default"


def test_clamp_keeps_optimized_weights_instead_of_discarding_them() -> None:
    from fsrs.scheduler import LOWER_BOUNDS_PARAMETERS, UPPER_BOUNDS_PARAMETERS

    inside = list(FSRS6_DEFAULT_PARAMETERS)
    assert clamp_fsrs_parameters(inside) == (inside, False)

    over = list(FSRS6_DEFAULT_PARAMETERS)
    over[4] = UPPER_BOUNDS_PARAMETERS[4] + 0.5  # e.g. an upgraded FSRS-4.5 w4 = 2*w5 + w4
    over[7] = LOWER_BOUNDS_PARAMETERS[7] - 1.0
    clamped, changed = clamp_fsrs_parameters(over)
    assert changed is True
    assert clamped[4] == UPPER_BOUNDS_PARAMETERS[4]
    assert clamped[7] == LOWER_BOUNDS_PARAMETERS[7]
    assert clamped[:4] == over[:4]
    Scheduler(parameters=clamped)  # accepted by py-fsrs


def test_upgraded_out_of_bounds_weights_are_clamped_not_defaulted(
    clock: datetime, tmp_path: Path
) -> None:
    """FSRS-4.5 set whose upgraded w4 = 2*w5 + w4 exceeds py-fsrs's bound of 10:
    Anki schedules with it as-is; we clamp rather than throw the set away."""
    four = [0.4, 0.9, 2.3, 10.9, 7.5, 1.5, 1.0, 0.01, 1.5, 0.1, 1.0, 1.9, 0.1, 0.3, 2.3, 0.2, 2.9]
    cfg = DeckConfigConfig(learn_steps=[1.0], relearn_steps=[10.0], fsrs_params_4=four)
    store, db_path = _make_store(tmp_path, deck_config=cfg)
    _insert_card(db_path, id=1, type=0, queue=0, due=1)

    result = store.answer_card(1, ease=3)

    assert result["fsrs_params"] == "fsrs4.5-upgraded-clamped"


def test_fsrs5_deck_config_no_longer_crashes_answer(clock: datetime, tmp_path: Path) -> None:
    """Collections last optimized on Anki 24.06-24.10 carry fsrs_params_5 only."""
    fsrs5 = [
        0.4,
        1.18,
        3.17,
        15.7,
        7.19,
        0.53,
        1.4,
        0.01,
        1.5,
        0.15,
        1.0,
        1.9,
        0.1,
        0.3,
        2.3,
        0.2,
        2.9,
        0.5,
        0.6,
    ]
    cfg = DeckConfigConfig(learn_steps=[1.0, 10.0], relearn_steps=[10.0], fsrs_params_5=fsrs5)
    store, db_path = _make_store(tmp_path, deck_config=cfg)
    _insert_card(db_path, id=1, type=2, queue=2, due=100, ivl=10, reps=3, factor=2500)
    _insert_revlog(db_path, rid=(NOW - 10 * 86400) * 1000, cid=1, ease=3, type_=1)

    result = store.answer_card(1, ease=3)

    assert result["answered"] is True
    assert result["fsrs_params"] == "fsrs5-upgraded"


def test_unusable_weight_sets_fall_back_to_defaults(clock: datetime, tmp_path: Path) -> None:
    # 23 weights: neither FSRS-4.5, 5 nor 6 -> defaults.
    cfg = DeckConfigConfig(learn_steps=[1.0], relearn_steps=[10.0], fsrs_params_6=[0.5] * 23)
    store, db_path = _make_store(tmp_path, deck_config=cfg)
    _insert_card(db_path, id=1, type=0, queue=0, due=1)

    result = store.answer_card(1, ease=3)

    assert result["fsrs_params"] == "default"
    # A corrupt FSRS-4.5 blob whose w5 makes the upgrade log undefined also defaults.
    bad = [1.0] * 17
    bad[5] = -1.0
    assert upgrade_fsrs_parameters(bad)[1] == "default"


# --- 3. deterministic fuzz ------------------------------------------------------


def test_fuzz_seed_matches_rslib_shape() -> None:
    assert fsrs_fuzz_seed(1_700_000_000_000, 7) == 1_700_000_000_007
    assert fsrs_fuzz_seed(1, 0) == 1


def test_preview_and_answer_agree_on_a_fuzzed_interval(clock: datetime, tmp_path: Path) -> None:
    """A mature review card gets an interval long enough for py-fsrs to fuzz; the
    preview must show exactly the interval answer_card then writes."""
    store, db_path = _make_store(tmp_path)
    data = json.dumps({"s": 80.0, "d": 4.0})
    _insert_card(db_path, id=1, type=2, queue=2, due=100, ivl=80, reps=12, factor=2500, data=data)
    _insert_revlog(db_path, rid=(NOW - 80 * 86400) * 1000, cid=1, ease=3, type_=1)

    import random

    random.seed(1)
    preview = {item["ease"]: item for item in store.preview_ratings(1)}
    # The answer normally happens in a *different* CLI process whose global RNG
    # was seeded from OS entropy; simulate that so only per-card seeding can pass.
    random.seed(2)
    result = store.answer_card(1, ease=3)

    assert result["interval"] == preview[3]["interval"]
    assert result["due"] == preview[3]["due"]
    assert _row(db_path, 1)["ivl"] == preview[3]["interval"]


def test_fuzz_is_actually_applied_and_deterministic(tmp_path: Path) -> None:
    """Seeding must not disable fuzz: repeated calls agree with each other, and
    the module-level random state is restored afterwards."""
    import random

    store, _ = _make_store(tmp_path)
    scheduler = Scheduler(learning_steps=[timedelta(minutes=1)], enable_fuzzing=True)
    now = datetime.fromtimestamp(NOW, tz=UTC)
    card = direct_mod.FSRSCard(
        card_id=42,
        state=State.Review,
        stability=200.0,
        difficulty=3.0,
        due=now,
        last_review=now - timedelta(days=200),
    )

    random.seed(123)
    before = random.getstate()
    a = store._review_with_fuzz_seed(
        scheduler, card, Rating.Good, review_datetime=now, card_id=42, reps=5
    )
    random.seed(456)  # a different "process"
    b = store._review_with_fuzz_seed(
        scheduler, card, Rating.Good, review_datetime=now, card_id=42, reps=5
    )
    random.setstate(before)
    c = store._review_with_fuzz_seed(
        scheduler, card, Rating.Good, review_datetime=now, card_id=42, reps=6
    )

    assert a.due == b.due
    assert random.getstate() == before  # caller's RNG untouched
    # A different rep count usually fuzzes differently; at minimum it must not crash.
    assert c.due is not None


# --- 4. revlog kind -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("type_", "queue", "due", "expected_kind"),
    [
        (0, 0, 1, REVLOG_KIND_LEARNING),  # new
        (1, 1, NOW - 30, REVLOG_KIND_LEARNING),  # learning
        (3, 1, NOW - 30, REVLOG_KIND_RELEARNING),  # relearning
        (2, 2, 100, REVLOG_KIND_REVIEW),  # review due today
        (2, 2, 90, REVLOG_KIND_REVIEW),  # overdue review
        (2, 2, 105, REVLOG_KIND_FILTERED),  # reviewed 5 days early
    ],
)
def test_revlog_kind_comes_from_state_before_the_answer(
    clock: datetime, tmp_path: Path, type_: int, queue: int, due: int, expected_kind: int
) -> None:
    store, db_path = _make_store(tmp_path)
    _insert_card(db_path, id=1, type=type_, queue=queue, due=due, ivl=10, reps=1, left=1001)
    if type_ in (2, 3):
        _insert_revlog(db_path, rid=(NOW - 10 * 86400) * 1000, cid=1, ease=3, type_=1)

    # Again on a review card is still a Review-kind entry (Anki), not Relearning.
    store.answer_card(1, ease=1 if type_ == 2 else 3)

    entry = _revlog(db_path, 1)[-1]
    assert entry["type"] == expected_kind


# --- 5. last review from revlog, no `lrt` -------------------------------------------


def test_last_review_time_reads_latest_real_review(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)
    _insert_revlog(db_path, rid=1_000_000, cid=1, ease=3, type_=1)
    _insert_revlog(db_path, rid=2_000_000, cid=1, ease=3, type_=1)
    # Manual reschedule (type 4) later must not count as a review.
    _insert_revlog(db_path, rid=3_000_000, cid=1, ease=0, type_=4)
    _insert_revlog(db_path, rid=9_000_000, cid=2, ease=3, type_=1)  # other card

    with store._connect() as conn:
        assert store._last_review_time(conn, 1) == datetime.fromtimestamp(2000, tz=UTC)
        assert store._last_review_time(conn, 3) is None


def test_answer_writes_lrt_like_anki_and_keeps_custom_data(clock: datetime, tmp_path: Path) -> None:
    """rslib CardData.last_review_time is serialized as "lrt" (seconds)."""
    store, db_path = _make_store(tmp_path)
    _insert_card(
        db_path,
        id=1,
        type=2,
        queue=2,
        due=100,
        ivl=10,
        reps=3,
        factor=2500,
        data=json.dumps({"s": 10.0, "d": 5.0, "lrt": 123, "cd": "keep"}),
    )

    store.answer_card(1, ease=3)

    data = json.loads(_row(db_path, 1)["data"])
    assert data["lrt"] == NOW
    assert data["cd"] == "keep"  # unrelated custom data preserved
    assert set(data) <= {"pos", "s", "d", "dr", "lrt", "cd"}  # only Anki CardData keys


def test_lrt_is_preferred_over_the_revlog_for_last_review(tmp_path: Path) -> None:
    """Anki prefers card.last_review_time; the revlog is the fallback for cards
    last answered by a version that predates the key."""
    store, db_path = _make_store(tmp_path)
    timing = sched_timing_today_v1(CRT, NOW)
    now = datetime.fromtimestamp(NOW, tz=UTC)
    _insert_card(
        db_path, id=1, type=2, queue=2, due=100, ivl=10, data=json.dumps({"lrt": NOW - 5 * 86400})
    )
    _insert_card(db_path, id=2, type=2, queue=2, due=100, ivl=10, data="{}")
    _insert_revlog(db_path, rid=(NOW - 21 * 86400) * 1000, cid=1, ease=3, type_=1)
    _insert_revlog(db_path, rid=(NOW - 20 * 86400) * 1000, cid=2, ease=3, type_=1)

    with_lrt = store._card_row_to_fsrs(_row(db_path, 1), timing=timing, now_dt=now)
    without = store._card_row_to_fsrs(_row(db_path, 2), timing=timing, now_dt=now)

    assert with_lrt.last_review == datetime.fromtimestamp(NOW - 5 * 86400, tz=UTC)
    assert without.last_review is None  # caller then consults the revlog
    with store._connect() as conn:
        assert store._last_review_time(conn, 2) == datetime.fromtimestamp(NOW - 20 * 86400, tz=UTC)


def test_stored_memory_state_is_used_when_revlog_supplies_last_review(
    clock: datetime, tmp_path: Path
) -> None:
    """With s/d in card data and a revlog entry, the card is not re-seeded (which
    would discard Anki's stored memory state)."""
    store, db_path = _make_store(tmp_path)
    _insert_card(
        db_path,
        id=1,
        type=2,
        queue=2,
        due=100,
        ivl=30,
        reps=3,
        factor=2500,
        data=json.dumps({"s": 30.0, "d": 5.0}),
    )
    _insert_revlog(db_path, rid=(NOW - 30 * 86400) * 1000, cid=1, ease=3, type_=1)
    seen: dict = {}
    original = store._seed_fsrs_card_from_revlog

    def spy(*args, **kwargs):
        seen["called"] = True
        return original(*args, **kwargs)

    store._seed_fsrs_card_from_revlog = spy  # type: ignore[method-assign]

    store.answer_card(1, ease=3)

    assert "called" not in seen
