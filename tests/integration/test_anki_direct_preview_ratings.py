from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import anki_cli.db.anki_direct as direct_mod
from anki_cli.db.anki_direct import AnkiDirectReadStore
from tests.conftest import Collection, new_collection


def _make_store(tmp_path: Path, *, col_crt: int = 0) -> tuple[AnkiDirectReadStore, Path]:
    """Bare schema-18 collection: no deck_config row, so preview uses the FSRS
    defaults exactly as the old fixture did."""
    col = new_collection(tmp_path / "collection.anki2", crt=col_crt, seed=False)
    col.insert_deck(id=1, name="Default")
    col.insert_notetype(id=10, name="Basic", fields=["Front", "Back"])
    col.insert_note(id=1000, fields=["Q", "A"])
    return col.store(writable=False), col.db_path


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
    odue: int = 0,
    odid: int = 0,
) -> None:
    Collection(db_path).insert_card(
        id=card_id, nid=1000, did=did, mod=mod, type=card_type, queue=queue, due=due, ivl=ivl,
        factor=factor, reps=1, odue=odue, odid=odid,
    )


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
        lambda row, *, timing, now_dt, **_steps: SimpleNamespace(
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
    today = store._today_due_index(int(direct_mod.time.time()))
    assert out[2]["due_info"] == {
        "kind": "review_day_index",
        "raw": 5,
        "day_index": 5,
        "epoch_secs": (10 + 5) * 86400,
        "days_from_today": 5 - today,
    }

    assert out[3]["type"] == 2
    assert out[3]["queue"] == 2
    assert out[3]["interval"] == 20
    assert out[3]["due_info"] == {
        "kind": "review_day_index",
        "raw": 6,
        "day_index": 6,
        "epoch_secs": (10 + 6) * 86400,
        "days_from_today": 6 - today,
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
        lambda row, *, timing, now_dt, **_steps: SimpleNamespace(
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
        lambda row, *, timing, now_dt, **_steps: SimpleNamespace(
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


def test_preview_ratings_refuses_a_card_in_a_preview_filtered_deck(tmp_path: Path) -> None:
    """Mirrors answer_card: don't offer ratings that answering would then refuse."""
    from anki_cli.proto.anki.decks import DeckFiltered, DeckKindContainer

    store, db_path = _make_store(tmp_path)
    Collection(db_path).insert_deck(
        id=555, name="Preview",
        kind=bytes(DeckKindContainer(filtered=DeckFiltered(reschedule=False))),
    )
    _insert_card(db_path, card_id=100, did=555, odid=1, odue=5, due=-7)

    with pytest.raises(ValueError, match="preview"):
        store.preview_ratings(100)
