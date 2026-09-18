from __future__ import annotations

from typing import Any

from anki_cli.core.scheduler import pick_next_due_card_id


class FakeBackend:
    def __init__(
        self,
        *,
        query_to_ids: dict[str, list[int]],
        card_due_map: dict[int, Any],
    ) -> None:
        self.query_to_ids = query_to_ids
        self.card_due_map = card_due_map
        self.find_calls: list[str] = []
        self.get_calls: list[int] = []

    def find_cards(self, query: str) -> list[int]:
        self.find_calls.append(query)
        return list(self.query_to_ids.get(query, []))

    def get_card(self, card_id: int) -> dict[str, Any]:
        self.get_calls.append(card_id)
        due = self.card_due_map.get(card_id)
        if isinstance(due, dict):
            return due
        return {"id": card_id, "due": due}


def test_pick_next_due_prefers_learning_due_first() -> None:
    backend = FakeBackend(
        query_to_ids={
            "is:learn is:due": [101, 102],
            "is:review is:due": [201],
            "is:new": [301],
        },
        card_due_map={101: 50, 102: 10, 201: 1, 301: 0},
    )

    card_id, kind = pick_next_due_card_id(backend)

    assert (card_id, kind) == (102, "learn_due")
    assert backend.find_calls[0] == "is:learn is:due"


def test_pick_next_due_falls_back_to_review_then_new() -> None:
    backend = FakeBackend(
        query_to_ids={
            "is:learn is:due": [],
            "is:review is:due": [201, 202],
            "is:new": [301],
        },
        card_due_map={201: 30, 202: 20, 301: 10},
    )

    card_id, kind = pick_next_due_card_id(backend)

    assert (card_id, kind) == (202, "review_due")
    assert backend.find_calls[:2] == ["is:learn is:due", "is:review is:due"]


def test_pick_next_due_returns_new_if_no_due_learning_or_review() -> None:
    backend = FakeBackend(
        query_to_ids={
            "is:learn is:due": [],
            "is:review is:due": [],
            "is:new": [301, 302],
        },
        card_due_map={301: 7, 302: 5},
    )

    card_id, kind = pick_next_due_card_id(backend)

    assert (card_id, kind) == (302, "new")


def test_pick_next_due_returns_none_when_no_cards() -> None:
    backend = FakeBackend(
        query_to_ids={
            "is:learn is:due": [],
            "is:review is:due": [],
            "is:new": [],
        },
        card_due_map={},
    )

    card_id, kind = pick_next_due_card_id(backend)

    assert (card_id, kind) == (None, "none")


def test_pick_next_due_ignores_cards_with_non_int_due() -> None:
    backend = FakeBackend(
        query_to_ids={
            "is:learn is:due": [101, 102],
            "is:review is:due": [201],
            "is:new": [],
        },
        card_due_map={
            101: None,
            102: "not-an-int",
            201: 99,
        },
    )

    card_id, kind = pick_next_due_card_id(backend)

    assert (card_id, kind) == (201, "review_due")


def test_pick_next_due_uses_scan_limit() -> None:
    backend = FakeBackend(
        query_to_ids={
            "is:learn is:due": [10, 20, 30],
            "is:review is:due": [],
            "is:new": [],
        },
        card_due_map={
            10: 100,
            20: 50,
            30: 1,  # would win, but excluded by scan_limit=2
        },
    )

    card_id, kind = pick_next_due_card_id(backend, scan_limit=2)

    assert (card_id, kind) == (20, "learn_due")
    assert backend.get_calls == [10, 20]


def test_pick_next_due_applies_deck_prefix_to_queries() -> None:
    backend = FakeBackend(
        query_to_ids={
            'deck:"Japanese::Core" is:learn is:due': [11],
            'deck:"Japanese::Core" is:review is:due': [],
            'deck:"Japanese::Core" is:new': [],
        },
        card_due_map={11: 3},
    )

    card_id, kind = pick_next_due_card_id(backend, deck="Japanese::Core")

    assert (card_id, kind) == (11, "learn_due")
    assert backend.find_calls == [
        'deck:"Japanese::Core" is:learn is:due',
    ]


def test_pick_next_due_compares_decoded_units_not_raw_due() -> None:
    """The ``is:learn is:due`` set mixes an intraday card (``due`` = epoch,
    ~1.7e9) with a day-learn card (``due`` = day index, ~2e4). Raw ``min(due)``
    always chose the day-learn card; comparing ``due_info`` puts the card that
    is due *sooner in time* first."""
    intraday_now = 1_700_000_000
    backend = FakeBackend(
        query_to_ids={"is:learn is:due": [1, 2]},
        card_due_map={
            # day-learn card: day index 40, i.e. 40 days after collection creation
            1: {"id": 1, "due": 40, "due_info": {"kind": "learn_day_index", "raw": 40,
                                                 "day_index": 40,
                                                 "epoch_secs": intraday_now - 3600}},
            # intraday card due right now
            2: {"id": 2, "due": intraday_now, "due_info": {"kind": "learn_epoch_secs",
                                                           "raw": intraday_now,
                                                           "epoch_secs": intraday_now}},
        },
    )

    card_id, kind = pick_next_due_card_id(backend)

    # Both have an epoch; the earlier epoch wins — regardless of raw ``due`` size.
    assert (card_id, kind) == (1, "learn_due")

    # Without an epoch for the day-learn card (AnkiConnect cannot compute one),
    # the intraday card — the only one with a known time — still comes first
    # instead of losing to a small raw day index.
    backend.card_due_map[1] = {"id": 1, "due": 40,
                               "due_info": {"kind": "learn_day_index", "raw": 40,
                                            "day_index": 40}}
    assert pick_next_due_card_id(backend) == (2, "learn_due")
