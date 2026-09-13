"""Anki's scheduling-day arithmetic, ported from ``rslib/src/scheduler/timing.rs``.

Anki does not count days as ``epoch // 86400``. The scheduling day rolls over
at a configurable local hour (``rollover``, default 4 am), and "days elapsed
since collection creation" is computed from calendar dates in the relevant
UTC offsets. Which algorithm applies depends on what the collection's
``config`` table contains:

* ``schedVer`` missing (v1 scheduler): plain ``(now - crt) // 86400``.
* ``schedVer`` set but no ``creationOffset`` (v2, legacy cutoff): the creation
  timestamp is snapped to the rollover hour in the *current* timezone, then
  whole days are counted.
* ``schedVer`` and ``creationOffset`` set (v2, new timezone handling, the
  default for collections created since 2.1.28): calendar-date difference
  between creation (in its own offset) and now (in the current offset), minus
  one if today's rollover has not yet passed.

All functions are pure so they can be tested against rslib's own vectors.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

SECONDS_PER_DAY = 86_400
DEFAULT_ROLLOVER_HOUR = 4


@dataclass(frozen=True)
class SchedTiming:
    """Timing information for "today" (rslib ``SchedTimingToday``).

    ``days_elapsed`` is the day index of today relative to collection creation,
    i.e. the value stored in ``cards.due`` for a review card due today.
    ``next_day_at`` is the epoch second at which the next scheduling day begins.
    """

    now: int
    days_elapsed: int
    next_day_at: int

    def day_start_epoch(self, day_index: int) -> int:
        """Epoch second at which scheduling day ``day_index`` began (or begins).

        Derived from ``next_day_at`` in whole 86 400 s steps, so far from today
        it drifts by the DST offset; that matches how Anki renders due dates.
        """
        return self.next_day_at + (day_index - self.days_elapsed - 1) * SECONDS_PER_DAY

    def day_index_for_epoch(self, epoch: int) -> int:
        """Scheduling day containing ``epoch``, relative to collection creation."""
        return self.days_elapsed + 1 + (epoch - self.next_day_at) // SECONDS_PER_DAY


def fixed_offset_from_minutes_west(minutes_west: int) -> timezone:
    """rslib ``fixed_offset_from_minutes``: Anki stores offsets as minutes *west* of UTC."""
    bounded = max(-23 * 60, min(23 * 60, int(minutes_west)))
    return timezone(timedelta(minutes=-bounded))


def local_minutes_west_for_stamp(epoch: int) -> int:
    """Minutes west of UTC in the local timezone at ``epoch`` (DST-aware)."""
    local = datetime.fromtimestamp(epoch, tz=UTC).astimezone()
    offset = local.utcoffset() or timedelta(0)
    return -int(offset.total_seconds() // 60)


def _rollover_datetime(moment: datetime, rollover_hour: int) -> datetime:
    return moment.replace(hour=rollover_hour % 24, minute=0, second=0, microsecond=0)


def _days_elapsed(start: datetime, end: datetime, rollover_passed: bool) -> int:
    days = end.toordinal() - start.toordinal()
    if not rollover_passed:
        days -= 1
    return max(0, days)


def sched_timing_today_v1(crt: int, now: int) -> SchedTiming:
    days_elapsed = (now - crt) // SECONDS_PER_DAY
    return SchedTiming(
        now=now,
        days_elapsed=max(0, days_elapsed),
        next_day_at=crt + (days_elapsed + 1) * SECONDS_PER_DAY,
    )


def sched_timing_today_v2_legacy(
    crt: int,
    rollover_hour: int,
    now: int,
    current_offset: timezone,
) -> SchedTiming:
    crt_at_rollover = int(
        _rollover_datetime(
            datetime.fromtimestamp(crt, tz=current_offset), rollover_hour
        ).timestamp()
    )
    days_elapsed = (now - crt_at_rollover) // SECONDS_PER_DAY
    next_day_at = int(
        _rollover_datetime(
            datetime.fromtimestamp(now, tz=current_offset), rollover_hour
        ).timestamp()
    )
    if next_day_at < now:
        next_day_at += SECONDS_PER_DAY
    return SchedTiming(now=now, days_elapsed=max(0, days_elapsed), next_day_at=next_day_at)


def sched_timing_today_v2_new(
    crt: int,
    creation_offset: timezone,
    now: int,
    current_offset: timezone,
    rollover_hour: int,
) -> SchedTiming:
    created_dt = datetime.fromtimestamp(crt, tz=creation_offset)
    now_dt = datetime.fromtimestamp(now, tz=current_offset)

    rollover_today = _rollover_datetime(now_dt, rollover_hour)
    rollover_passed = rollover_today <= now_dt
    next_day_at = int(
        (rollover_today + timedelta(days=1)).timestamp()
        if rollover_passed
        else rollover_today.timestamp()
    )
    return SchedTiming(
        now=now,
        days_elapsed=_days_elapsed(created_dt, now_dt, rollover_passed),
        next_day_at=next_day_at,
    )


def sched_timing_today(
    *,
    crt: int,
    now: int,
    creation_minutes_west: int | None,
    current_minutes_west: int,
    rollover_hour: int | None,
) -> SchedTiming:
    """Pick the timing algorithm the way rslib ``sched_timing_today`` does."""
    if rollover_hour is None:
        return sched_timing_today_v1(crt, now)
    current_offset = fixed_offset_from_minutes_west(current_minutes_west)
    if creation_minutes_west is None:
        return sched_timing_today_v2_legacy(crt, rollover_hour, now, current_offset)
    return sched_timing_today_v2_new(
        crt,
        fixed_offset_from_minutes_west(creation_minutes_west),
        now,
        current_offset,
        rollover_hour,
    )
