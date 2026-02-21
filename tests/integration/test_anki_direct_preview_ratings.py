from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import anki_cli.db.anki_direct as direct_mod
from anki_cli.db.anki_direct import AnkiDirectReadStore


def _make_store(tmp_path: Path, *, col_crt: int = 0) -> tuple[AnkiDirectReadStore, Path]:
    db_path = tmp_path / "collection.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE col (
            crt INTEGER NOT NULL
        );

        CREATE TABLE cards (
            id INTEGER PRIMARY KEY,
            nid INTEGER NOT NULL,
            did INTEGER NOT NULL,
            ord INTEGER NOT NULL,
            mod INTEGER NOT NULL,
            usn INTEGER NOT NULL,
            type INTEGER NOT NULL,
            queue INTEGER NOT NULL,
            due INTEGER NOT NULL,
            ivl INTEGER NOT NULL,
            factor INTEGER NOT NULL,
            reps INTEGER NOT NULL,
            lapses INTEGER NOT NULL,
            left INTEGER NOT NULL,
            odue INTEGER NOT NULL,
            odid INTEGER NOT NULL,
            flags INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO col (crt) VALUES (?)", (col_crt,))
    conn.commit()
    conn.close()

    return AnkiDirectReadStore(db_path), db_path


def _insert_card(
    db_path: Path,
    *,
    card_id: int,
    did: int = 1,
    mod: int = 100,
    card_type: int = 2,
    queue: int = 2,
    due: int = 5,
    ivl: int = 10,
    factor: int = 2500,
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO cards (
            id, nid, did, ord, mod, usn, type, queue, due, ivl, factor, reps,
            lapses, left, odue, odid, flags, data
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            card_id,
            1000,  # nid
            did,
            0,     # ord
            mod,
            0,     # usn
            card_type,
            queue,
            due,
            ivl,
            factor,
            1,     # reps
            0,     # lapses
            0,     # left
            0,     # odue
            0,     # odid
            0,     # flags
            "{}",
        ),
    )
    conn.commit()
    conn.close()


def test_preview_ratings_missing_card_raises_lookup_error(tmp_path: Path) -> None:
    store, _db_path = _make_store(tmp_path)

    with pytest.raises(LookupError, match="Card not found"):
        store.preview_ratings(999)


def test_preview_ratings_returns_four_ease_options_with_decoded_due_info(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # col_crt day index = 10 for review due epoch calculations
    store, db_path = _make_store(tmp_path, col_crt=864000)
    _insert_card(db_path, card_id=200, factor=2500)

    class FakeScheduler:
        def review_card(self, card, rating, review_datetime):
            ease = int(rating)
            state_by_ease = {
                1: direct_mod.State.Relearning,
                2: direct_mod.State.Learning,
                3: direct_mod.State.Review,
                4: direct_mod.State.Review,
            }
            return (SimpleNamespace(state=state_by_ease[ease], ease_marker=ease), None)

    monkeypatch.setattr(
        store,
        "_build_scheduler",
        lambda conn, deck_id: (FakeScheduler(), 0.9, 2, 1),
    )
    monkeypatch.setattr(
        store,
        "_card_row_to_fsrs",
        lambda row, *, col_crt_sec, now_dt: SimpleNamespace(
            state=direct_mod.State.Review,
            step=None,
            stability=None,
            difficulty=None,
            last_review=None,
        ),
    )
    monkeypatch.setattr(
        store,
        "_seed_fsrs_card_from_revlog",
        lambda *args, **kwargs: SimpleNamespace(
            stability=3.2,
            difficulty=6.7,
            last_review=datetime(2020, 1, 1, tzinfo=UTC),
        ),
    )

    mapped = {
        1: (3, 1, 1_700_000_001, 0, 2002, 1_700_000_001),
        2: (1, 1, 1_700_000_002, 0, 1001, 1_700_000_002),
        3: (2, 2, 5, 12, 0, 1_700_000_003),
        4: (2, 2, 6, 20, 0, 1_700_000_004),
    }
    monkeypatch.setattr(
        store,
        "_map_fsrs_result_to_anki",
        lambda **kwargs: mapped[kwargs["next_card"].ease_marker],
    )

    out = store.preview_ratings(200)

    assert [item["ease"] for item in out] == [1, 2, 3, 4]

    assert out[0]["type"] == 3
    assert out[0]["queue"] == 1
    assert out[0]["due"] == 1_700_000_001
    assert out[0]["state"] == str(direct_mod.State.Relearning)
    assert out[0]["due_info"] == {
        "kind": "learn_epoch_secs",
        "raw": 1_700_000_001,
        "epoch_secs": 1_700_000_001,
    }

    assert out[1]["type"] == 1
    assert out[1]["queue"] == 1
    assert out[1]["state"] == str(direct_mod.State.Learning)

    assert out[2]["type"] == 2
    assert out[2]["queue"] == 2
    assert out[2]["interval"] == 12
    assert out[2]["due_info"] == {
        "kind": "review_day_index",
        "raw": 5,
        "day_index": 5,
        "epoch_secs": (10 + 5) * 86400,
    }

    assert out[3]["type"] == 2
    assert out[3]["queue"] == 2
    assert out[3]["interval"] == 20
    assert out[3]["due_info"] == {
        "kind": "review_day_index",
        "raw": 6,
        "day_index": 6,
        "epoch_secs": (10 + 6) * 86400,
    }


def test_preview_ratings_sets_relearning_step_zero_when_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    _insert_card(db_path, card_id=201)

    seen_steps: list[int | None] = []

    class FakeScheduler:
        def review_card(self, card, rating, review_datetime):
            seen_steps.append(card.step)
            return (SimpleNamespace(state=direct_mod.State.Learning, ease_marker=int(rating)), None)

    monkeypatch.setattr(
        store,
        "_build_scheduler",
        lambda conn, deck_id: (FakeScheduler(), 0.9, 2, 1),
    )
    monkeypatch.setattr(
        store,
        "_card_row_to_fsrs",
        lambda row, *, col_crt_sec, now_dt: SimpleNamespace(
            state=direct_mod.State.Relearning,
            step=None,
            stability=2.0,
            difficulty=5.0,
            last_review=datetime(2020, 1, 1, tzinfo=UTC),
        ),
    )
    monkeypatch.setattr(
        store,
        "_map_fsrs_result_to_anki",
        lambda **kwargs: (1, 1, 1_700_000_000, 0, 1001, 1_700_000_000),
    )

    out = store.preview_ratings(201)

    assert len(out) == 4
    assert seen_steps == [0, 0, 0, 0]


def test_preview_ratings_falls_back_when_seed_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    _insert_card(
        db_path,
        card_id=202,
        mod=1_600_000_000,
        ivl=30,
        factor=2500,
        card_type=2,
        queue=2,
    )

    observed: dict[str, Any] = {}

    class FakeScheduler:
        def review_card(self, card, rating, review_datetime):
            observed["stability"] = card.stability
            observed["difficulty"] = card.difficulty
            observed["last_review"] = card.last_review
            return (SimpleNamespace(state=direct_mod.State.Review, ease_marker=int(rating)), None)

    monkeypatch.setattr(
        store,
        "_build_scheduler",
        lambda conn, deck_id: (FakeScheduler(), 0.9, 2, 1),
    )
    monkeypatch.setattr(
        store,
        "_card_row_to_fsrs",
        lambda row, *, col_crt_sec, now_dt: SimpleNamespace(
            state=direct_mod.State.Review,
            step=0,
            stability=None,
            difficulty=None,
            last_review=None,
        ),
    )
    monkeypatch.setattr(store, "_seed_fsrs_card_from_revlog", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        store,
        "_map_fsrs_result_to_anki",
        lambda **kwargs: (2, 2, 7, 15, 0, 1_700_000_100),
    )

    out = store.preview_ratings(202)

    assert len(out) == 4
    assert observed["stability"] == pytest.approx(30.0)
    assert observed["difficulty"] is not None
    assert 1.0 <= float(observed["difficulty"]) <= 10.0
    assert observed["last_review"] is not None