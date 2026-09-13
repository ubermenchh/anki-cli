"""Port of the tests in rslib/src/scheduler/timing.rs; the expected values are Anki's."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from anki_cli.db import timing as t

AEST_MINS_WEST = -600
MDT = timezone(timedelta(hours=-6))
MST = timezone(timedelta(hours=-7))
MDT_WEST = 6 * 60
MST_WEST = 7 * 60


def _ts(tz: timezone, y: int, mo: int, d: int, h: int, mi: int = 0, s: int = 0) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=tz).timestamp())


def _elap(start: int, end: int, start_west: int, end_west: int, rollover: int) -> int:
    return t.sched_timing_today_v2_new(
        start,
        t.fixed_offset_from_minutes_west(start_west),
        end,
        t.fixed_offset_from_minutes_west(end_west),
        rollover,
    ).days_elapsed


def test_fixed_offset_minutes_west() -> None:
    off = t.fixed_offset_from_minutes_west(AEST_MINS_WEST)
    assert off.utcoffset(None) == timedelta(hours=10)
    # capped
    assert t.fixed_offset_from_minutes_west(99_999).utcoffset(None) == timedelta(hours=-23)


def test_days_elapsed_basic_rollover_rules() -> None:
    tz = MDT
    crt = _ts(tz, 2019, 12, 1, 2, 0, 0)
    west = MDT_WEST

    # days can't be negative
    assert _elap(crt, crt, west, west, 4) == 0
    assert _elap(crt, crt - 86_400, west, west, 4) == 0
    # 2am the next day is still the same day
    assert _elap(crt, crt + 24 * 3600, west, west, 4) == 0
    # day rolls over at 4am
    assert _elap(crt, crt + 26 * 3600, west, west, 4) == 1
    # the longest extra delay is +23, or 19 hours past the 4 hour default
    assert _elap(crt, crt + (26 + 18) * 3600, west, west, 23) == 0
    assert _elap(crt, crt + (26 + 19) * 3600, west, west, 23) == 1


def test_days_elapsed_across_dst_change_is_stable() -> None:
    # a collection created @ midnight in MDT in the past
    crt = _ts(MDT, 2018, 8, 6, 0, 0, 0)
    # with the current time being MST
    now = _ts(MST, 2019, 12, 26, 20, 0, 0)
    assert _elap(crt, now, MDT_WEST, MST_WEST, 4) == 507
    # the number shouldn't change with a DST offset change
    assert _elap(crt, now, MDT_WEST, MDT_WEST, 4) == 507


def test_days_elapsed_creation_at_3am_counts_from_next_rollover() -> None:
    # collection created at 3am on the 6th, so day 1 starts at 4am on the 7th,
    # and day 3 on the 9th.
    crt = _ts(MDT, 2018, 8, 6, 3, 0, 0)
    assert _elap(crt, _ts(MST, 2018, 8, 9, 1, 59, 59), MDT_WEST, MST_WEST, 4) == 2
    assert _elap(crt, _ts(MST, 2018, 8, 9, 3, 59, 59), MDT_WEST, MST_WEST, 4) == 2
    assert _elap(crt, _ts(MST, 2018, 8, 9, 4, 0, 0), MDT_WEST, MST_WEST, 4) == 3


@pytest.mark.parametrize("creation_hour", [0, 1, 4, 12, 22, 23])
@pytest.mark.parametrize("current_day", [0, 1, 2, 3])
@pytest.mark.parametrize("current_hour", [0, 1, 4, 12, 22, 23])
@pytest.mark.parametrize("rollover_hour", [0, 1, 4, 12, 22, 23])
def test_days_elapsed_matrix(
    creation_hour: int, current_day: int, current_hour: int, rollover_hour: int
) -> None:
    crt = _ts(MDT, 2018, 8, 6, creation_hour, 0, 0)
    end = _ts(MDT, 2018, 8, 6 + current_day, current_hour, 0, 0)
    expected = max(1, current_day) - 1 if current_hour < rollover_hour else current_day
    assert _elap(crt, end, MDT_WEST, MDT_WEST, rollover_hour) == expected


def test_next_day_at_before_and_after_rollover() -> None:
    tz = MDT
    west = MDT_WEST
    rollhour = 4
    crt = _ts(tz, 2019, 1, 1, 2, 0, 0)

    def timing(now: int) -> t.SchedTiming:
        return t.sched_timing_today_v2_new(
            crt,
            t.fixed_offset_from_minutes_west(west),
            now,
            t.fixed_offset_from_minutes_west(west),
            rollhour,
        )

    # before the rollover, the next day should be later on the same day
    assert timing(_ts(tz, 2019, 1, 3, 2)).next_day_at == _ts(tz, 2019, 1, 3, rollhour)
    # at / after the rollover, the next day should be the next day
    assert timing(_ts(tz, 2019, 1, 3, rollhour)).next_day_at == _ts(tz, 2019, 1, 4, rollhour)
    assert timing(_ts(tz, 2019, 1, 3, rollhour + 3)).next_day_at == _ts(tz, 2019, 1, 4, rollhour)


def test_legacy_timing_vectors() -> None:
    now = 1584491078

    assert t.sched_timing_today_v1(1575226800, now) == t.SchedTiming(
        now=now, days_elapsed=107, next_day_at=1584558000
    )

    aest = t.fixed_offset_from_minutes_west(AEST_MINS_WEST)
    assert t.sched_timing_today_v2_legacy(1533564000, 0, now, aest) == t.SchedTiming(
        now=now, days_elapsed=589, next_day_at=1584540000
    )
    assert t.sched_timing_today_v2_legacy(1524038400, 4, now, aest) == t.SchedTiming(
        now=now, days_elapsed=700, next_day_at=1584554400
    )


def test_dispatch_picks_algorithm_like_rslib() -> None:
    now = 1584491078
    # rollover unset -> v1
    assert t.sched_timing_today(
        crt=1575226800,
        now=now,
        creation_minutes_west=None,
        current_minutes_west=0,
        rollover_hour=None,
    ) == t.sched_timing_today_v1(1575226800, now)
    # rollover set, creation offset unset -> v2 legacy
    assert (
        t.sched_timing_today(
            crt=1524038400,
            now=now,
            creation_minutes_west=None,
            current_minutes_west=AEST_MINS_WEST,
            rollover_hour=4,
        ).days_elapsed
        == 700
    )
    # both set -> v2 new
    crt = _ts(MDT, 2018, 8, 6, 3, 0, 0)
    assert (
        t.sched_timing_today(
            crt=crt,
            now=_ts(MST, 2018, 8, 9, 4, 0, 0),
            creation_minutes_west=MDT_WEST,
            current_minutes_west=MST_WEST,
            rollover_hour=4,
        ).days_elapsed
        == 3
    )


def test_issue_21_reproduction_est_rollover() -> None:
    """crt 2023-11-14 04:00 EST, now 2023-11-15 01:00 EST: 21 h since rollover -> day 0.
    Plain UTC-midnight flooring gave day 1."""
    est_west = 5 * 60
    crt = 1_699_952_400
    now = 1_700_028_000
    assert now // 86_400 - crt // 86_400 == 1  # the old, wrong answer
    timing = t.sched_timing_today(
        crt=crt,
        now=now,
        creation_minutes_west=est_west,
        current_minutes_west=est_west,
        rollover_hour=4,
    )
    assert timing.days_elapsed == 0
    assert timing.next_day_at == crt + 86_400  # 04:00 EST the next day


def test_day_start_epoch_and_day_index_round_trip() -> None:
    tz = MDT
    crt = _ts(tz, 2019, 1, 1, 2, 0, 0)
    now = _ts(tz, 2019, 1, 10, 12, 0, 0)  # day 9, rollover passed
    timing = t.sched_timing_today_v2_new(crt, tz, now, tz, 4)
    assert timing.days_elapsed == 9
    assert timing.day_start_epoch(9) == _ts(tz, 2019, 1, 10, 4)
    assert timing.day_start_epoch(10) == timing.next_day_at
    assert timing.day_start_epoch(0) == _ts(tz, 2019, 1, 1, 4)
    for idx in (0, 5, 9, 10, 40):
        assert timing.day_index_for_epoch(timing.day_start_epoch(idx)) == idx
        assert timing.day_index_for_epoch(timing.day_start_epoch(idx) + 86_399) == idx
    assert timing.day_index_for_epoch(now) == 9


def test_local_minutes_west_matches_datetime() -> None:
    now = 1_700_000_000
    expected = -int(
        (datetime.fromtimestamp(now).astimezone().utcoffset() or timedelta()).total_seconds() // 60
    )
    assert t.local_minutes_west_for_stamp(now) == expected
