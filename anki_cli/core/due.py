"""Decode ``cards.due`` / ``cards.left`` into comparable units.

Anki packs three different quantities into ``cards.due`` depending on the
card's type and queue — a position (new cards), a unix epoch second (intraday
learning, queue 1) or a day index relative to the collection's creation
(review and day-learn). rslib tells learning epochs from day indices with a
fixed threshold (``Card::restore_queue_from_type``): any epoch after
2001-09-09 exceeds it and no plausible day index ever will.

Both backends produce the same ``due_info`` shape from this module (see
``models/entities.py::DueInfo``). Converting a day index to an epoch needs the
collection's rollover timing, which only the direct backend has; callers pass
``timing=None`` when they do not.
"""

from __future__ import annotations

from typing import Protocol

from anki_cli.models.output import JSONValue

LEARN_DUE_EPOCH_THRESHOLD = 1_000_000_000


class DayTiming(Protocol):
    """The subset of ``db.timing.SchedTiming`` needed to place a day index in time."""

    days_elapsed: int

    def day_start_epoch(self, day_index: int) -> int: ...


def is_intraday_learn_due(due: int) -> bool:
    return due > LEARN_DUE_EPOCH_THRESHOLD


def decode_due(
    *,
    card_type: int,
    queue: int,
    due_raw: int,
    timing: DayTiming | None,
) -> dict[str, JSONValue]:
    """``due_info`` for a card. Keyed on ``card_type`` like rslib, not queue."""
    if card_type == 0:
        return {"kind": "new_position", "raw": due_raw, "position": due_raw}

    if card_type in (1, 3):
        if is_intraday_learn_due(due_raw):
            return {"kind": "learn_epoch_secs", "raw": due_raw, "epoch_secs": due_raw}
        # Day-learn (queue 3): a learning step of >= 1 day stores a day index.
        out_learn: dict[str, JSONValue] = {
            "kind": "learn_day_index",
            "raw": due_raw,
            "day_index": due_raw,
        }
        if timing is not None:
            out_learn["epoch_secs"] = timing.day_start_epoch(due_raw)
            out_learn["days_from_today"] = due_raw - timing.days_elapsed
        return out_learn

    if card_type == 2:
        out: dict[str, JSONValue] = {
            "kind": "review_day_index",
            "raw": due_raw,
            "day_index": due_raw,
        }
        if timing is not None:
            out["epoch_secs"] = timing.day_start_epoch(due_raw)
            # Relative day count for display; epoch_secs is the *start* of the
            # due scheduling day, so flooring (epoch - now) would be off by one.
            out["days_from_today"] = due_raw - timing.days_elapsed
        return out

    return {"kind": "raw", "raw": due_raw, "queue": queue, "type": card_type}


def decode_left(left_raw: int) -> dict[str, int]:
    """``left_info``: Anki packs ``today_remaining * 1000 + until_graduation``."""
    if left_raw < 0:
        return {"raw": left_raw}
    return {
        "raw": left_raw,
        "today_remaining": left_raw // 1000,
        "until_graduation": left_raw % 1000,
    }


def due_sort_key(due_info: dict[str, JSONValue] | None, *, fallback: int) -> tuple[int, int]:
    """Order cards by "what would Anki show first", the same on both backends.

    Returns ``(bucket, value)``. Buckets follow Anki's own queue order —
    intraday learning ahead of everything (Anki v3 shows learning steps as
    they fall due and interleaves day-learn with reviews), then day-indexed
    cards (review and day-learn) by day, then new cards by position, then
    anything undecoded by its raw value. Within a bucket the units agree,
    which a raw ``min(due)`` across queues could not guarantee (an intraday
    epoch ~1.7e9 always lost to a day index ~2e4).

    Ranking by *kind* rather than by whichever unit happens to be available
    is what keeps the two backends in step: the direct backend knows the
    epoch of a day-index card, AnkiConnect does not, and if that decided the
    bucket the same collection would pick different cards per backend.
    """
    if not isinstance(due_info, dict):
        return (3, fallback)
    kind = due_info.get("kind")
    epoch = due_info.get("epoch_secs")
    day = due_info.get("day_index")
    pos = due_info.get("position")
    if kind == "learn_epoch_secs" and isinstance(epoch, int):
        return (0, epoch)
    if isinstance(day, int):
        return (1, day)
    if isinstance(pos, int):
        return (2, pos)
    raw = due_info.get("raw")
    return (3, raw if isinstance(raw, int) else fallback)
