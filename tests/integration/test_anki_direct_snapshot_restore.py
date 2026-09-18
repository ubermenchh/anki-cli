from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import anki_cli.db.anki_direct as direct_mod
from anki_cli.db.anki_direct import AnkiDirectReadStore
from tests.anki_schema import connect
from tests.conftest import new_collection, seed_review_card


def _make_store_with_cards_revlog(tmp_path: Path) -> tuple[AnkiDirectReadStore, Path]:
    """Bare schema-18 collection with review card 100 already flagged ``usn=7``
    (so a restore that re-flags it is observable)."""
    col = new_collection(tmp_path / "collection.anki2", seed=False)
    seed_review_card(col, ivl=15, usn=7, data='{"x":1}')
    return col.store(writable=False), col.db_path


def _make_answer_store(tmp_path: Path) -> tuple[AnkiDirectReadStore, Path]:
    """The same card test_anki_direct_answer_card seeds (ivl 10, usn 0)."""
    col = new_collection(tmp_path / "collection.anki2", seed=False)
    seed_review_card(col)
    return col.store(writable=False), col.db_path


def _card_row(db_path: Path, card_id: int) -> dict[str, Any]:
    conn = connect(str(db_path))
    row = conn.execute(
        """
        SELECT id, did, ord, type, queue, due, ivl, factor,
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


def _insert_revlog_row(
    db_path: Path, *, row_id: int, cid: int, ease: int = 3, usn: int = -1
) -> None:
    conn = connect(str(db_path))
    conn.execute(
        """
        INSERT INTO revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, type)
        VALUES (?, ?, ?, ?, 10, 5, 2500, 0, 1)
        """,
        (row_id, cid, usn, ease),
    )
    conn.commit()
    conn.close()


def test_snapshot_card_state_returns_expected_fields(tmp_path: Path) -> None:
    store, _db_path = _make_store_with_cards_revlog(tmp_path)

    snap = store.snapshot_card_state(100)

    # The snapshot is a pure projection of the cards row; the id of the revlog
    # row to delete on undo is merged in by the caller after answer_card.
    assert snap == {
        "id": 100,
        "did": 1,
        "odid": 0,
        "odue": 0,
        "ord": 0,
        "type": 2,
        "queue": 2,
        "due": 30,
        "ivl": 15,
        "factor": 2500,
        "reps": 20,
        "lapses": 1,
        "left": 0,
        "flags": 3,
        "data": '{"x":1}',
    }


def test_snapshot_card_state_missing_card_raises_lookup(tmp_path: Path) -> None:
    store, _db_path = _make_store_with_cards_revlog(tmp_path)

    with pytest.raises(LookupError, match="Card not found"):
        store.snapshot_card_state(999)


def test_restore_card_state_updates_card_and_deletes_recorded_revlog_row(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store_with_cards_revlog(tmp_path)

    monkeypatch.setattr(direct_mod.time, "time", lambda: 1234.567)

    # Older revlog row; preserved.
    _insert_revlog_row(db_path, row_id=1000, cid=100)
    # The row the undone review wrote; deleted via snapshot["revlog_id"].
    _insert_revlog_row(db_path, row_id=5000, cid=100)
    # A *later* unsynced row for the same card (e.g. reviewed again in Anki
    # Desktop afterwards); must survive because it is not the recorded row.
    _insert_revlog_row(db_path, row_id=6000, cid=100)
    # Row for a different card; untouched.
    _insert_revlog_row(db_path, row_id=7000, cid=200)

    snapshot = {
        "id": 100,
        "did": 9,
        "ord": 2,
        "type": 1,
        "queue": 3,
        "due": 98765,
        "ivl": 42,
        "factor": 1900,
        "reps": 33,
        "lapses": 4,
        "left": 2002,
        "flags": 1,
        "data": '{"restored":true}',
        "revlog_id": 5000,
    }

    result = store.restore_card_state(snapshot)

    assert result == {"card_id": 100, "restored": True, "revlog_deleted": 1}

    row = _card_row(db_path, 100)
    assert row["did"] == 9
    assert row["ord"] == 2
    assert row["type"] == 1
    assert row["queue"] == 3
    assert row["due"] == 98765
    assert row["ivl"] == 42
    assert row["factor"] == 1900
    assert row["reps"] == 33
    assert row["lapses"] == 4
    assert row["left"] == 2002
    assert row["flags"] == 1
    assert row["data"] == '{"restored":true}'
    assert row["mod"] == 1234  # int(time.time())
    assert row["usn"] == -1

    revlog = _revlog_rows(db_path)
    assert [r["id"] for r in revlog] == [1000, 6000, 7000]
    assert all(r["type"] == 1 for r in revlog)


def test_restore_card_state_does_not_delete_synced_revlog_row(
    tmp_path: Path,
) -> None:
    store, db_path = _make_store_with_cards_revlog(tmp_path)

    # The recorded row has already synced (usn != -1). Deleting it locally
    # would diverge from AnkiWeb because revlog deletions never propagate.
    _insert_revlog_row(db_path, row_id=5000, cid=100, usn=7)

    result = store.restore_card_state({"id": 100, "revlog_id": 5000})

    assert result == {"card_id": 100, "restored": True, "revlog_deleted": 0}
    assert [r["id"] for r in _revlog_rows(db_path)] == [5000]


def test_restore_card_state_revlog_id_scoped_to_card(tmp_path: Path) -> None:
    store, db_path = _make_store_with_cards_revlog(tmp_path)

    # The recorded revlog id belongs to a different card.
    _insert_revlog_row(db_path, row_id=5000, cid=200)

    result = store.restore_card_state({"id": 100, "revlog_id": 5000})

    assert result == {"card_id": 100, "restored": True, "revlog_deleted": 0}
    assert [r["id"] for r in _revlog_rows(db_path)] == [5000]


def test_restore_card_state_ignores_non_int_revlog_id(tmp_path: Path) -> None:
    store, db_path = _make_store_with_cards_revlog(tmp_path)

    _insert_revlog_row(db_path, row_id=5000, cid=100)

    for bad in (True, "5000", 5000.0, None):
        result = store.restore_card_state({"id": 100, "revlog_id": bad})
        assert result == {"card_id": 100, "restored": True, "revlog_deleted": 0}

    assert [r["id"] for r in _revlog_rows(db_path)] == [5000]


def test_restore_card_state_without_revlog_id_leaves_revlog(
    tmp_path: Path,
) -> None:
    store, db_path = _make_store_with_cards_revlog(tmp_path)

    _insert_revlog_row(db_path, row_id=1000, cid=100)
    _insert_revlog_row(db_path, row_id=5000, cid=100)

    result = store.restore_card_state(
        {
            "id": 100,
            "did": 1,
            "ord": 0,
            "type": 2,
            "queue": 2,
            "due": 30,
            "ivl": 15,
            "factor": 2500,
            "reps": 20,
            "lapses": 1,
            "left": 0,
            "flags": 0,
            "data": "",
        }
    )

    assert result == {"card_id": 100, "restored": True, "revlog_deleted": 0}
    assert [r["id"] for r in _revlog_rows(db_path)] == [1000, 5000]


def test_restore_card_state_missing_id_type_raises_value_error(tmp_path: Path) -> None:
    store, _db_path = _make_store_with_cards_revlog(tmp_path)

    with pytest.raises(ValueError, match=r"snapshot.id must be an int"):
        store.restore_card_state({"id": "100"})


def test_restore_card_state_missing_target_card_returns_restored_false(
    tmp_path: Path,
) -> None:
    store, db_path = _make_store_with_cards_revlog(tmp_path)

    result = store.restore_card_state(
        {
            "id": 999,  # does not exist
            "did": 1,
            "ord": 0,
            "type": 0,
            "queue": 0,
            "due": 0,
            "ivl": 0,
            "factor": 0,
            "reps": 0,
            "lapses": 0,
            "left": 0,
            "flags": 0,
            "data": "",
            "revlog_id": 5000,
        }
    )

    assert result == {"card_id": 999, "restored": False, "revlog_deleted": 0}
    assert _revlog_rows(db_path) == []


def _fake_answer_internals(monkeypatch: pytest.MonkeyPatch, store: AnkiDirectReadStore) -> None:
    """Stub the FSRS internals so answer_card is deterministic.

    ``_allocate_epoch_ms_id`` is deliberately left alone so the revlog row gets
    a real wall-clock id; restore must not depend on how that id relates to
    when the snapshot was taken.
    """
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
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
            state=direct_mod.State.Learning,
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


def test_restore_deletes_the_undone_reviews_revlog_row(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_answer_store(tmp_path)
    _fake_answer_internals(monkeypatch, store)

    snap = store.snapshot_card_state(100)
    result = store.answer_card(100, ease=3)

    rows = _revlog_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["id"] == result["revlog_id"]
    assert rows[0]["usn"] == -1

    # What the undo push records: the card-row projection plus the id of the
    # revlog row this answer wrote.
    restore = store.restore_card_state({**snap, "revlog_id": result["revlog_id"]})

    assert restore["revlog_deleted"] == 1
    assert _revlog_rows(db_path) == []


def test_restore_leaves_revlog_row_that_synced_after_the_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_answer_store(tmp_path)
    _fake_answer_internals(monkeypatch, store)

    snap = store.snapshot_card_state(100)
    result = store.answer_card(100, ease=3)

    # The review's revlog row synced to AnkiWeb before the undo ran.
    conn = connect(str(db_path))
    conn.execute("UPDATE revlog SET usn = 5 WHERE id = ?", (result["revlog_id"],))
    conn.commit()
    conn.close()

    restore = store.restore_card_state({**snap, "revlog_id": result["revlog_id"]})

    assert restore["revlog_deleted"] == 0
    assert [r["id"] for r in _revlog_rows(db_path)] == [result["revlog_id"]]
