"""Regression tests for #21: the scheduling day follows Anki's rollover hour and
timezone handling, not UTC midnight.

The pure arithmetic is covered by tests/unit/test_timing.py (rslib's vectors);
these tests check that the store reads the right config keys and that the
result reaches the public read/write paths.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import anki_cli.db.anki_direct as direct_mod
from anki_cli.db.anki_direct import AnkiDirectReadStore
from tests.conftest import Collection, new_collection

EST_WEST = 5 * 60
# Issue #21 reproduction values: crt 2023-11-14 04:00 EST, now 2023-11-15 01:00 EST.
CRT = 1_699_952_400
NOW = 1_700_028_000


def _make_store(
    tmp_path: Path,
    *,
    crt: int = CRT,
    config: dict[str, object] | None = None,
    with_config_table: bool = True,
) -> tuple[AnkiDirectReadStore, Path]:
    """``with_config_table=False`` drops Anki's ``config`` table after building
    the real schema: not an Anki shape, but the store tolerates it (v1 fallback)
    and one test pins that."""
    col = new_collection(tmp_path / "collection.anki2", crt=crt, seed=False)
    col.insert_deck(id=1, name="Default")
    col.insert_notetype(id=10, name="Basic", fields=["Front", "Back"])
    col.insert_note(id=1000, fields=["Q", "A"])
    if with_config_table:
        for key, value in (config or {}).items():
            col.set_config(key, value)
    else:
        col.execute("DROP TABLE config")
    return col.store(writable=False), col.db_path


def _insert_card(db_path: Path, *, card_id: int, type_: int, queue: int, due: int) -> None:
    Collection(db_path).insert_card(id=card_id, nid=1000, type=type_, queue=queue, due=due, mod=0)


@pytest.fixture
def est_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin wall clock to the issue's `now` and the machine's zone to EST."""
    monkeypatch.setattr(direct_mod.time, "time", lambda: NOW)
    monkeypatch.setattr(direct_mod, "local_minutes_west_for_stamp", lambda _epoch: EST_WEST)


V2_NEW = {"schedVer": 2, "rollover": 4, "creationOffset": EST_WEST}


def test_v2_new_timing_reads_config_and_uses_rollover(est_clock: None, tmp_path: Path) -> None:
    store, _ = _make_store(tmp_path, config=V2_NEW)

    # 21 hours since the 04:00 rollover: still day 0. UTC flooring said day 1.
    assert NOW // 86400 - CRT // 86400 == 1
    assert store._today_due_index(NOW) == 0

    with store._connect() as conn:
        timing = store._timing(conn, NOW)
    assert timing.next_day_at == CRT + 86400  # next 04:00 EST


def test_review_due_tomorrow_is_not_counted_today(est_clock: None, tmp_path: Path) -> None:
    """The user-visible symptom from #21: cards due 'tomorrow' showed up today."""
    store, db_path = _make_store(tmp_path, config=V2_NEW)
    _insert_card(db_path, card_id=1, type_=2, queue=2, due=1)  # due on day 1
    _insert_card(db_path, card_id=2, type_=2, queue=2, due=0)  # due today (day 0)
    _insert_card(db_path, card_id=3, type_=1, queue=3, due=1)  # day-learn tomorrow

    assert store.get_due_counts() == {"new": 0, "learn": 0, "review": 1, "total": 1}
    assert store.get_next_due_card() == {"card_id": 2, "kind": "review_due"}


def test_rollover_hour_is_honored(est_clock: None, tmp_path: Path) -> None:
    """With rollover at 00:00 the same instant is already day 1."""
    store, db_path = _make_store(tmp_path, config={**V2_NEW, "rollover": 0})
    _insert_card(db_path, card_id=1, type_=2, queue=2, due=1)

    assert store._today_due_index(NOW) == 1
    assert store.get_due_counts()["review"] == 1


def test_missing_rollover_defaults_to_4(est_clock: None, tmp_path: Path) -> None:
    store, _ = _make_store(tmp_path, config={"schedVer": 2, "creationOffset": EST_WEST})
    assert store._today_due_index(NOW) == 0


def test_v2_legacy_when_creation_offset_missing(est_clock: None, tmp_path: Path) -> None:
    """schedVer without creationOffset: crt snapped to the rollover hour in the
    current zone, then whole days (rslib sched_timing_today_v2_legacy)."""
    store, _ = _make_store(tmp_path, config={"schedVer": 2, "rollover": 4})
    # crt is exactly 04:00 EST, so crt_at_rollover == crt and 21 h elapsed -> 0.
    assert store._today_due_index(NOW) == 0
    # ... and a moment past the next 04:00 EST is day 1.
    with store._connect() as conn:
        assert store._timing(conn, CRT + 86400).days_elapsed == 1


def test_v1_fallback_without_sched_ver_matches_old_behaviour(
    est_clock: None, tmp_path: Path
) -> None:
    """No schedVer (v1 scheduler, or a stripped test fixture): plain 86400-second
    days counted from crt, which is what the code did before #21."""
    store, _ = _make_store(tmp_path, config={})
    assert store._today_due_index(NOW) == (NOW - CRT) // 86400 == 0
    with store._connect() as conn:
        assert store._timing(conn, CRT + 2 * 86400 + 1).days_elapsed == 2


def test_no_config_table_at_all_falls_back_to_v1(est_clock: None, tmp_path: Path) -> None:
    store, _ = _make_store(tmp_path, with_config_table=False)
    assert store._today_due_index(NOW) == 0


def test_due_info_epoch_uses_scheduling_day_start(est_clock: None, tmp_path: Path) -> None:
    store, _ = _make_store(tmp_path, config=V2_NEW)
    with store._connect() as conn:
        timing = store._timing(conn, NOW)

    info = store._decode_due(card_type=2, queue=2, due_raw=3, timing=timing)
    # Day 3 starts at the 04:00 EST rollover three days after creation day 0.
    assert info["epoch_secs"] == CRT + 3 * 86400
    assert info["epoch_secs"] == timing.day_start_epoch(3)


def test_current_offset_comes_from_the_machine_clock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Anki desktop uses the local zone at `now`; a DST shift changes the offset
    but must not change days_elapsed for a collection created in the other half."""
    store, _ = _make_store(
        tmp_path, config={"schedVer": 2, "rollover": 4, "creationOffset": 6 * 60}
    )
    # rslib vector: created midnight MDT 2018-08-06, now 20:00 MST 2019-12-26 -> 507.
    crt = 1_533_535_200
    now = 1_577_415_600
    conn = sqlite3.connect(str(store.db_path))
    conn.execute("UPDATE col SET crt = ?", (crt,))
    conn.commit()
    conn.close()

    monkeypatch.setattr(direct_mod, "local_minutes_west_for_stamp", lambda _e: 7 * 60)  # MST
    assert store._today_due_index(now) == 507
    monkeypatch.setattr(direct_mod, "local_minutes_west_for_stamp", lambda _e: 6 * 60)  # MDT
    assert store._today_due_index(now) == 507
