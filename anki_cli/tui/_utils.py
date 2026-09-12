"""Small helpers shared by the TUI apps."""

from __future__ import annotations

import time


def _relative_eta(epoch_secs: int) -> str:
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
