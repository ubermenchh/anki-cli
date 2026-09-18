from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fsrs import State

import anki_cli.db.scheduling as scheduling_mod
from anki_cli.db.store import AnkiDirectStore
from anki_cli.proto.anki.decks import DeckFiltered, DeckKindContainer
from tests.anki_schema import connect
from tests.conftest import Collection, new_collection, seed_review_card


def _make_store(tmp_path: Path) -> tuple[AnkiDirectStore, Path]:
    """Bare schema-18 collection (no deck_config row — tests assert the FSRS
    fallback when it is absent) with one review card, id 100, in deck 1."""
    col = new_collection(tmp_path / "collection.anki2", seed=False)
    seed_review_card(col)
    return col.store(writable=False), col.db_path


def _assert_card_synced(db_path: Path, card_id: int = 100) -> dict[str, Any]:
    """usn flagged, card mod moved off the seeded 111, col.mod moved (#47)."""
    return Collection(db_path).assert_synced("cards", card_id, baseline=111)


def _card_row(db_path: Path, card_id: int) -> dict[str, Any]:
    conn = connect(str(db_path))
    row = conn.execute(
        """
        SELECT id, did, odid, odue, ord, type, queue, due, ivl, factor,
               reps, lapses, left, flags, data, mod, usn
        FROM cards
        WHERE id = ?
        """,
        (card_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    return dict(row)


def _revlog_rows(db_path: Path) -> list[dict[str, Any]]:
    conn = connect(str(db_path))
    rows = conn.execute(
        "SELECT id, cid, usn, ease, ivl, lastIvl, factor, time, type FROM revlog ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def test_answer_card_invalid_ease_raises_value_error(tmp_path: Path) -> None:
    store, _db_path = _make_store(tmp_path)

    with pytest.raises(ValueError, match="ease must be one of"):
        store.answer_card(100, ease=9)


def test_answer_card_missing_card_raises_lookup_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, _db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    with pytest.raises(LookupError, match="Card not found"):
        store.answer_card(999, ease=3)


def test_answer_card_updates_card_and_writes_revlog_non_lapse(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)

    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(store, "_allocate_epoch_ms_id", lambda conn, table: 9001)

    # Keep flow deterministic and independent of FSRS internals.
    monkeypatch.setattr(
        store,
        "_build_scheduler",
        lambda conn, deck_id: (
            type(
                "FakeScheduler",
                (),
                {
                    "review_card": lambda self, card, rating, review_datetime: (
                        SimpleNamespace(stability=3.2, difficulty=6.7),
                        None,
                    )
                },
            )(),
            0.9,
            2,
            1,
        ),
    )
    monkeypatch.setattr(
        store,
        "_card_row_to_fsrs",
        lambda row, *, timing, now_dt, **_steps: SimpleNamespace(
            state=State.Learning,
            step=0,
            stability=None,
            difficulty=None,
            last_review=None,
        ),
    )
    monkeypatch.setattr(
        store,
        "_map_fsrs_result_to_anki",
        lambda **kwargs: (2, 2, 33, 44, 0, 123456),
    )

    result = store.answer_card(100, ease=3)

    assert result == {
        "card_id": 100,
        "ease": 3,
        "answered": True,
        "fsrs_params": "default",  # deck_config table present, no row for this deck
        "queue": 2,
        "type": 2,
        "due": 33,
        "interval": 44,
        "revlog_id": 9001,
    }

    row = _card_row(db_path, 100)
    assert row["type"] == 2
    assert row["queue"] == 2
    assert row["due"] == 33
    assert row["ivl"] == 44
    assert row["reps"] == 21
    assert row["lapses"] == 1  # ease != 1
    assert row["left"] == 0
    _assert_card_synced(db_path)

    data = json.loads(row["data"])
    assert data["pos"] == 0
    assert data["dr"] == 0.9
    assert data["s"] == 3.2
    assert data["d"] == 6.7
    assert data["lrt"] == row["mod"]  # rslib CardData.last_review_time, seconds

    revlog = _revlog_rows(db_path)
    assert revlog == [
        {
            "id": 9001,
            "cid": 100,
            "usn": -1,
            "ease": 3,
            "ivl": 44,
            "lastIvl": 10,
            "factor": 670,  # round(6.7 * 100)
            "time": 0,
            "type": 1,  # review
        }
    ]


def test_answer_card_lapse_increments_lapses_and_sets_relearn_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)

    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(store, "_allocate_epoch_ms_id", lambda conn, table: 9002)

    monkeypatch.setattr(
        store,
        "_build_scheduler",
        lambda conn, deck_id: (
            type(
                "FakeScheduler",
                (),
                {
                    "review_card": lambda self, card, rating, review_datetime: (
                        SimpleNamespace(stability=4.0, difficulty=5.5),
                        None,
                    )
                },
            )(),
            0.9,
            2,
            1,
        ),
    )
    monkeypatch.setattr(
        store,
        "_card_row_to_fsrs",
        lambda row, *, timing, now_dt, **_steps: SimpleNamespace(
            state=State.Learning,
            step=0,
            stability=None,
            difficulty=None,
            last_review=None,
        ),
    )
    monkeypatch.setattr(
        store,
        "_map_fsrs_result_to_anki",
        lambda **kwargs: (3, 2, 50, 60, 0, 123500),
    )

    result = store.answer_card(100, ease=1)

    assert result["answered"] is True
    assert result["type"] == 3
    assert result["queue"] == 2
    assert result["due"] == 50
    assert result["interval"] == 60

    row = _assert_card_synced(db_path)
    assert row["reps"] == 21
    assert row["lapses"] == 2  # incremented when ease == 1

    revlog = _revlog_rows(db_path)
    assert revlog == [
        {
            "id": 9002,
            "cid": 100,
            "usn": -1,
            "ease": 1,
            "ivl": 60,
            "lastIvl": 10,
            "factor": 550,
            "time": 0,
            # rslib RevlogReviewKind is the card's state *before* the answer: a
            # review card answered Again is still logged as Review (1), and the
            # relearning kind (2) is only used once the card is in relearning.
            "type": 1,
        }
    ]


def test_answer_card_day_learn_step_writes_day_index_and_positive_revlog_ivl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A >= 1-day learning step lands in queue 3 with a day-index due, and the
    revlog logs day-learn intervals in positive days (rslib as_revlog_interval)."""
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(store, "_allocate_epoch_ms_id", lambda conn, table: 9003)

    # Fixture col.crt is 0, so "today" is a large day index; pin the clock.
    now_sec = 1_700_000_000
    today = now_sec // 86400
    monkeypatch.setattr(time, "time", lambda: now_sec)

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return datetime.fromtimestamp(now_sec, tz=UTC)

    monkeypatch.setattr(scheduling_mod, "datetime", _Now)

    # Card is already in day-learn: due tomorrow-ish (today + 1) -> lastIvl +1.
    conn = connect(str(db_path))
    conn.execute(
        "UPDATE cards SET type = 1, queue = 3, due = ?, left = 1001 WHERE id = 100",
        (today + 1,),
    )
    conn.commit()
    conn.close()

    two_days_later = datetime.fromtimestamp(now_sec + 2 * 86400, tz=UTC)
    monkeypatch.setattr(
        store,
        "_build_scheduler",
        lambda conn, deck_id: (
            type(
                "FakeScheduler",
                (),
                {
                    "review_card": lambda self, card, rating, review_datetime: (
                        SimpleNamespace(
                            state=State.Learning,
                            step=1,
                            due=two_days_later,
                            stability=None,
                            difficulty=None,
                        ),
                        None,
                    )
                },
            )(),
            0.9,
            3,
            1,
        ),
    )

    result = store.answer_card(100, ease=3)

    assert result["queue"] == 3
    assert result["due"] == today + 2
    row = _assert_card_synced(db_path)
    assert (row["type"], row["queue"], row["due"]) == (1, 3, today + 2)

    (entry,) = _revlog_rows(db_path)
    assert entry["ivl"] == 2  # positive days for day-learn
    assert entry["lastIvl"] == 1  # previous due was today + 1


# --- filtered-deck awareness (#20) --------------------------------------------


def _insert_deck(db_path: Path, *, did: int, name: str, filtered: bool, reschedule: bool = True):
    col = Collection(db_path)
    if filtered:
        # Bare filtered deck (no search terms), as before.
        kind = bytes(DeckKindContainer(filtered=DeckFiltered(reschedule=reschedule)))
        col.insert_deck(id=did, name=name, kind=kind)
    else:
        col.insert_deck(id=did, name=name)


def _park_card_in_filtered_deck(db_path: Path, *, filtered_did: int, home_did: int, odue: int):
    conn = connect(str(db_path))
    conn.execute(
        "UPDATE cards SET did = ?, odid = ?, odue = ?, due = -7 WHERE id = 100",
        (filtered_did, home_did, odue),
    )
    conn.commit()
    conn.close()


def _fake_scheduler(monkeypatch: pytest.MonkeyPatch, store: AnkiDirectStore, captured: dict):
    class FakeScheduler:
        def review_card(self, card, rating, review_datetime):
            captured["fsrs_due"] = card.due
            return SimpleNamespace(stability=3.2, difficulty=6.7), None

    def build(conn, deck_id):
        captured["scheduler_deck"] = deck_id
        return FakeScheduler(), 0.9, 2, 1

    monkeypatch.setattr(store, "_build_scheduler", build)
    monkeypatch.setattr(
        store,
        "_map_fsrs_result_to_anki",
        lambda **kwargs: (2, 2, 33, 44, 0, 123456),
    )


def test_answer_card_in_rescheduling_filtered_deck_sends_it_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Anki v3: remove_from_filtered_deck_before_reschedule, then schedule normally."""
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(store, "_allocate_epoch_ms_id", lambda conn, table: 9010)
    _insert_deck(db_path, did=1, name="Home", filtered=False)
    _insert_deck(db_path, did=555, name="Cram", filtered=True, reschedule=True)
    _park_card_in_filtered_deck(db_path, filtered_did=555, home_did=1, odue=30)
    captured: dict = {}
    _fake_scheduler(monkeypatch, store, captured)

    store.answer_card(100, ease=3)

    row = _assert_card_synced(db_path)
    assert (row["did"], row["odid"], row["odue"]) == (1, 0, 0)
    assert (row["type"], row["queue"], row["due"]) == (2, 2, 33)
    # Options come from the home deck, and FSRS saw the real due (odue = day 30
    # after crt 0), not the filtered-deck position -7.
    assert captured["scheduler_deck"] == 1
    assert captured["fsrs_due"] == datetime.fromtimestamp(30 * 86400, tz=UTC)


def test_answer_card_in_preview_filtered_deck_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    _insert_deck(db_path, did=1, name="Home", filtered=False)
    _insert_deck(db_path, did=555, name="Preview", filtered=True, reschedule=False)
    _park_card_in_filtered_deck(db_path, filtered_did=555, home_did=1, odue=30)
    before = _card_row(db_path, 100)

    with pytest.raises(ValueError, match="preview"):
        store.answer_card(100, ease=3)

    assert _card_row(db_path, 100) == before
    assert _revlog_rows(db_path) == []
    Collection(db_path).assert_untouched()  # a refusal leaves col.mod alone


def test_undo_restores_filtered_deck_membership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """snapshot -> answer (card goes home) -> restore must put it back on loan."""
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    ids = iter([9011, 9012])
    monkeypatch.setattr(store, "_allocate_epoch_ms_id", lambda conn, table: next(ids))
    _insert_deck(db_path, did=1, name="Home", filtered=False)
    _insert_deck(db_path, did=555, name="Cram", filtered=True)
    _park_card_in_filtered_deck(db_path, filtered_did=555, home_did=1, odue=30)
    _fake_scheduler(monkeypatch, store, {})

    snapshot = store.snapshot_card_state(100)
    assert (snapshot["did"], snapshot["odid"], snapshot["odue"]) == (555, 1, 30)

    store.answer_card(100, ease=3)
    assert _card_row(db_path, 100)["did"] == 1

    store.restore_card_state(snapshot)

    row = _assert_card_synced(db_path)
    assert (row["did"], row["odid"], row["odue"], row["due"]) == (555, 1, 30, -7)


def test_answer_card_new_card_on_loan_records_original_position(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """data.pos must be the new-queue position (odue), not the filtered-deck slot."""
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(store, "_allocate_epoch_ms_id", lambda conn, table: 9020)
    _insert_deck(db_path, did=1, name="Home", filtered=False)
    _insert_deck(db_path, did=555, name="Cram", filtered=True)
    conn = connect(str(db_path))
    conn.execute(
        "UPDATE cards SET type = 0, queue = 0, did = 555, odid = 1, odue = 12, due = -99999 "
        "WHERE id = 100"
    )
    conn.commit()
    conn.close()
    _fake_scheduler(monkeypatch, store, {})

    store.answer_card(100, ease=3)

    data = json.loads(_card_row(db_path, 100)["data"])
    assert data["pos"] == 12
