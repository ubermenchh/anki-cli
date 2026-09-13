from __future__ import annotations

import pytest

import anki_cli.tui._utils as utils_mod

_NOW = 1_700_000_000


@pytest.mark.parametrize(
    ("delta_secs", "expected"),
    [
        (-60, "<1m"),      # past timestamps clamp to zero
        (0, "<1m"),
        (59, "<1m"),
        (60, "1m"),
        (3599, "59m"),
        (3600, "1h"),
        (86399, "23h"),
        (86400, "1d"),
        (36 * 3600, "2d"),  # pins the ceiling: 1.5d rounds up, not down
    ],
)
def test_relative_eta(
    monkeypatch: pytest.MonkeyPatch, delta_secs: int, expected: str
) -> None:
    monkeypatch.setattr(utils_mod.time, "time", lambda: _NOW)
    assert utils_mod.relative_eta(_NOW + delta_secs) == expected


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        (5, 0, 5),
        ("7", 0, 7),
        (None, 3, 3),
        ("nope", -1, -1),
        ([], 9, 9),
    ],
)
def test_to_int(value: object, default: int, expected: int) -> None:
    assert utils_mod.to_int(value, default) == expected


@pytest.mark.parametrize(
    ("delta_secs", "expected"),
    [
        (-86400, "today"),   # past epochs clamp to zero days
        (0, "today"),
        (86399, "tomorrow"),  # epoch_secs is the *start* of the due day: ceil
        (86400, "tomorrow"),
        (86400 + 60, "2d"),
        (2 * 86400, "2d"),
        (10 * 86400, "10d"),
    ],
)
def test_due_day_label_from_epoch(
    monkeypatch: pytest.MonkeyPatch, delta_secs: int, expected: str
) -> None:
    monkeypatch.setattr(utils_mod.time, "time", lambda: _NOW)
    due_info = {"kind": "review_day_index", "epoch_secs": _NOW + delta_secs}
    assert utils_mod.due_day_label(due_info) == expected


@pytest.mark.parametrize(
    ("days_from_today", "expected"),
    [(-3, "today"), (0, "today"), (1, "tomorrow"), (4, "4d")],
)
def test_due_day_label_prefers_days_from_today(
    monkeypatch: pytest.MonkeyPatch, days_from_today: int, expected: str
) -> None:
    """The backend's relative count wins over the epoch fallback."""
    monkeypatch.setattr(utils_mod.time, "time", lambda: _NOW)
    due_info = {
        "kind": "review_day_index",
        "epoch_secs": _NOW + 30 * 86400,  # stale/wrong epoch must be ignored
        "days_from_today": days_from_today,
    }
    assert utils_mod.due_day_label(due_info) == expected


@pytest.mark.parametrize(
    ("due_info", "expected"),
    [
        ({"kind": "review_day_index", "day_index": 42}, "d42"),
        ({"kind": "learn_day_index", "day_index": 0}, "d0"),
        ({"kind": "review_day_index"}, "review"),
        ({"kind": "review_day_index", "day_index": "x"}, "review"),
    ],
)
def test_due_day_label_from_day_index(due_info: dict, expected: str) -> None:
    assert utils_mod.due_day_label(due_info) == expected
