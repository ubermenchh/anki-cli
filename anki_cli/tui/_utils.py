"""Small helpers shared by the TUI apps."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def relative_eta(epoch_secs: int) -> str:
    now = int(time.time())
    delta = max(0, int(epoch_secs) - now)
    if delta < 60:
        return "<1m"
    minutes = delta // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = (hours + 23) // 24
    return f"{days}d"


def due_day_label(due_info: Mapping[str, Any]) -> str:
    """Label for review/learn day-index ``due_info``: today/tomorrow/Nd.

    Prefers the backend's relative ``days_from_today`` count; the
    ``epoch_secs`` fallback treats it as the *start* of the due day, so it
    rounds up rather than flooring (a rollover 21 h away is tomorrow, not
    today). Falls back to ``d<day_index>`` when only a raw day index is
    present.
    """
    days = due_info.get("days_from_today")
    if not isinstance(days, int):
        epoch = due_info.get("epoch_secs")
        if isinstance(epoch, int):
            days = max(0, -((int(time.time()) - epoch) // 86400))
        else:
            day_index = due_info.get("day_index")
            return f"d{day_index}" if isinstance(day_index, int) else "review"
    if days <= 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"{days}d"
