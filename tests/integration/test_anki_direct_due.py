from __future__ import annotations

import sqlite3
from pathlib import Path

from anki_cli.db.anki_direct import AnkiDirectReadStore


def _make_store(
    tmp_path: Path,
    *,
    decks: list[tuple[int, str]],
    cards: list[tuple[int, int, int, int]],
) -> AnkiDirectReadStore:
    db_path = tmp_path / "collection.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE col (
            crt INTEGER NOT NULL
        );

        CREATE TABLE decks (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        );

        CREATE TABLE cards (
            id INTEGER PRIMARY KEY,
            did INTEGER NOT NULL,
            queue INTEGER NOT NULL,
            due INTEGER NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO col (crt) VALUES (0)")
    conn.executemany("INSERT INTO decks (id, name) VALUES (?, ?)", decks)
    conn.executemany("INSERT INTO cards (id, did, queue, due) VALUES (?, ?, ?, ?)", cards)
    conn.commit()
    conn.close()

    return AnkiDirectReadStore(db_path)


def test_get_due_counts_all_decks_counts_only_due_cards(tmp_path: Path) -> None:
    store = _make_store(
        tmp_path,
        decks=[(1, "DeckA"), (2, "DeckB")],
        cards=[
            (1, 1, 0, 999),          # new (always counted)
            (2, 1, 1, 0),            # intraday learn due (epoch)
            (3, 1, 1, 10**12),       # intraday learn not due
            (4, 1, 3, 0),            # day-learn due (day index 0)
            (7, 1, 3, 10**6),        # day-learn not due (day index far ahead) -- #19
            (5, 2, 2, 0),            # review due
            (6, 2, 2, 10**9),        # review not due
        ],
    )

    assert store.get_due_counts() == {
        "new": 1,
        "learn": 2,
        "review": 1,
        "total": 4,
    }


def test_get_due_counts_deck_filter_is_exact_name_match(tmp_path: Path) -> None:
    store = _make_store(
        tmp_path,
        decks=[(1, "DeckA"), (2, "DeckA::Child"), (3, "DeckB")],
        cards=[
            (1, 1, 0, 0),
            (2, 1, 1, 0),
            (3, 2, 2, 0),
            (4, 3, 0, 0),
        ],
    )

    assert store.get_due_counts(deck="DeckA") == {
        "new": 1,
        "learn": 1,
        "review": 0,
        "total": 2,
    }

    assert store.get_due_counts(deck="MissingDeck") == {
        "new": 0,
        "learn": 0,
        "review": 0,
        "total": 0,
    }


def test_get_next_due_card_prefers_learning_before_review_and_new(tmp_path: Path) -> None:
    store = _make_store(
        tmp_path,
        decks=[(1, "DeckA")],
        cards=[
            (10, 1, 2, 0),   # review due
            (11, 1, 0, 0),   # new
            (12, 1, 1, 100), # learn due (should win by priority)
        ],
    )

    assert store.get_next_due_card() == {"card_id": 12, "kind": "learn_due"}


def test_get_next_due_card_learning_uses_due_then_id_order(tmp_path: Path) -> None:
    # crt = 0, so a day-learn (queue 3) due of N means epoch N * 86400.
    store = _make_store(
        tmp_path,
        decks=[(1, "DeckA")],
        cards=[
            (50, 1, 1, 86_400 * 2),  # intraday learn, epoch = day 2
            (40, 1, 1, 86_400 * 2),  # same instant, lower id -> should win
            (30, 1, 3, 3),           # day-learn due day 3 (later than day 2)
            (60, 1, 1, 86_400 * 4),  # intraday learn, day 4
        ],
    )

    assert store.get_next_due_card() == {"card_id": 40, "kind": "learn_due"}


def test_get_next_due_card_orders_day_learn_by_absolute_time(tmp_path: Path) -> None:
    """Regression for #19: a day-learn due is a day index, not an epoch."""
    store = _make_store(
        tmp_path,
        decks=[(1, "DeckA")],
        cards=[
            (50, 1, 1, 86_400 * 5),  # intraday learn due on day 5
            (40, 1, 3, 1),           # day-learn due on day 1 -> earlier, wins
        ],
    )

    assert store.get_next_due_card() == {"card_id": 40, "kind": "learn_due"}


def test_get_next_due_card_skips_day_learn_not_yet_due(tmp_path: Path) -> None:
    """A day-learn card 30 days out must not be treated as an epoch from 1970."""
    store = _make_store(
        tmp_path,
        decks=[(1, "DeckA")],
        cards=[
            (40, 1, 3, 10**6),  # day index far in the future
            (10, 1, 2, 0),      # review due today
        ],
    )

    assert store.get_next_due_card() == {"card_id": 10, "kind": "review_due"}


def test_get_next_due_card_falls_back_review_then_new(tmp_path: Path) -> None:
    store_review = _make_store(
        tmp_path / "review_case",
        decks=[(1, "DeckA")],
        cards=[
            (20, 1, 1, 10**12), # learn not due
            (21, 1, 2, 0),      # review due
            (22, 1, 0, 0),      # new
        ],
    )
    assert store_review.get_next_due_card() == {"card_id": 21, "kind": "review_due"}

    store_new = _make_store(
        tmp_path / "new_case",
        decks=[(1, "DeckA")],
        cards=[
            (30, 1, 1, 10**12), # learn not due
            (31, 1, 2, 10**9),  # review not due
            (32, 1, 0, 5),      # new
        ],
    )
    assert store_new.get_next_due_card() == {"card_id": 32, "kind": "new"}


def test_get_next_due_card_returns_none_when_no_candidates(tmp_path: Path) -> None:
    store = _make_store(
        tmp_path,
        decks=[(1, "DeckA")],
        cards=[
            (40, 1, 1, 10**12), # learn not due
            (41, 1, 2, 10**9),  # review not due
        ],
    )

    assert store.get_next_due_card() == {"card_id": None, "kind": "none"}
    assert store.get_next_due_card(deck="MissingDeck") == {"card_id": None, "kind": "none"}


def test_get_next_due_card_respects_deck_filter(tmp_path: Path) -> None:
    store = _make_store(
        tmp_path,
        decks=[(1, "DeckA"), (2, "DeckB")],
        cards=[
            (100, 1, 1, 0), # DeckA learn due
            (200, 2, 1, 0), # DeckB learn due
        ],
    )

    assert store.get_next_due_card(deck="DeckB") == {"card_id": 200, "kind": "learn_due"}