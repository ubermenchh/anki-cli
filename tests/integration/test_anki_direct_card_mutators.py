from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import anki_cli.db.anki_direct as direct_mod
from anki_cli.db.anki_direct import AnkiDirectReadStore
from anki_cli.proto.anki.decks import DeckFiltered, DeckKindContainer, DeckNormal
from tests.anki_schema import connect
from tests.conftest import COL_BASE_MOD_MS, Collection, new_collection

_NORMAL_KIND = bytes(DeckKindContainer(normal=DeckNormal(config_id=1)))
_FILTERED_KIND = bytes(DeckKindContainer(filtered=DeckFiltered(reschedule=True)))


def _make_store(tmp_path: Path) -> tuple[AnkiDirectReadStore, Path]:
    col = new_collection(tmp_path / "collection.anki2", seed=False)
    for did, name in ((1, "Default"), (2, "Target"), (10, "Lang"), (11, "Lang::Child"),
                      (12, "Other")):
        col.insert_deck(id=did, name=name, kind=_NORMAL_KIND)
    col.insert_deck(id=555, name="Cram", kind=_FILTERED_KIND)
    col.insert_notetype(id=10, name="Basic", fields=["Front", "Back"])
    col.insert_note(id=1000, fields=["Q", "A"])
    return col.store(writable=False), col.db_path


def _insert_card(
    db_path: Path,
    *,
    card_id: int,
    did: int = 1,
    card_type: int = 2,
    queue: int = 2,
    due: int = 0,
    ivl: int = 10,
    factor: int = 2500,
    reps: int = 0,
    lapses: int = 0,
    left: int = 0,
    odue: int = 0,
    odid: int = 0,
    data: str = "{}",
    mod: int = 1,
    usn: int = 0,
    flags: int = 0,
) -> None:
    Collection(db_path).insert_card(
        id=card_id, nid=1000, did=did, type=card_type, queue=queue, due=due, ivl=ivl,
        factor=factor, reps=reps, lapses=lapses, left=left, odue=odue, odid=odid, data=data,
        mod=mod, usn=usn, flags=flags,
    )


def _assert_col_synced(db_path: Path) -> None:
    """A landed write moves col.mod so sync scans for usn = -1 rows (#47)."""
    assert Collection(db_path).col()["mod"] > COL_BASE_MOD_MS


def _card_row(db_path: Path, card_id: int) -> dict[str, Any]:
    conn = connect(str(db_path))
    row = conn.execute(
        """
        SELECT
            id, did, type, queue, due, ivl, factor, reps, lapses, left,
            odue, odid, data, mod, usn, flags
        FROM cards
        WHERE id = ?
        """,
        (card_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    return dict(row)


def test_move_cards_updates_existing_ids_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_000_000)

    _insert_card(db_path, card_id=10, did=1)
    _insert_card(db_path, card_id=30, did=1)

    result = store.move_cards(card_ids=[30, 10, 30, 999, 0, -1], deck="Target")

    assert result == {"moved": 2, "card_ids": [10, 30, 999], "deck": "Target"}

    row10 = _card_row(db_path, 10)
    row30 = _card_row(db_path, 30)
    assert row10["did"] == 2
    assert row30["did"] == 2
    assert row10["mod"] == 1_000_000
    assert row30["mod"] == 1_000_000
    assert row10["usn"] == -1
    assert row30["usn"] == -1
    _assert_col_synced(db_path)


def test_move_cards_unknown_deck_raises_lookup_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    _insert_card(db_path, card_id=10, did=1)

    with pytest.raises(LookupError, match="Deck not found"):
        store.move_cards(card_ids=[10], deck="MissingDeck")

    assert _card_row(db_path, 10)["did"] == 1


def test_set_card_flag_validates_flag_range(tmp_path: Path) -> None:
    store, _db_path = _make_store(tmp_path)

    with pytest.raises(ValueError, match=r"flag must be in range 0..7"):
        store.set_card_flag(card_ids=[1], flag=-1)

    with pytest.raises(ValueError, match=r"flag must be in range 0..7"):
        store.set_card_flag(card_ids=[1], flag=8)


def test_set_card_flag_updates_existing_ids_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_000_000)

    _insert_card(db_path, card_id=1, flags=0)
    _insert_card(db_path, card_id=2, flags=4)

    result = store.set_card_flag(card_ids=[2, 1, 2, 999], flag=7)

    assert result == {"updated": 2, "card_ids": [1, 2, 999], "flag": 7}

    row1 = _card_row(db_path, 1)
    row2 = _card_row(db_path, 2)
    assert row1["flags"] == 7
    assert row2["flags"] == 7
    assert row1["mod"] == 1_000_000
    assert row2["mod"] == 1_000_000
    assert row1["usn"] == -1
    assert row2["usn"] == -1
    _assert_col_synced(db_path)


def test_bury_then_unbury_all_restores_queue_by_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_000_000)

    _insert_card(db_path, card_id=1, card_type=0, queue=0)  # new
    _insert_card(db_path, card_id=2, card_type=2, queue=2)  # review
    # Buried relearn with a day-index due -> day-learn queue (3) on unbury.
    _insert_card(db_path, card_id=3, card_type=3, queue=-2, due=20_050)
    # Buried learn with an epoch due -> intraday learn queue (1) on unbury.
    _insert_card(db_path, card_id=4, card_type=1, queue=-3, due=1_700_000_300)

    buried = store.bury_cards(card_ids=[2, 1, 2, 999])
    assert buried == {"buried": 2, "card_ids": [1, 2, 999]}
    assert _card_row(db_path, 1)["queue"] == -2
    assert _card_row(db_path, 2)["queue"] == -2

    unburied = store.unbury_cards()
    _assert_col_synced(db_path)
    assert unburied == {"unburied": 4, "scope": "all"}

    assert _card_row(db_path, 1)["queue"] == 0
    assert _card_row(db_path, 2)["queue"] == 2
    assert _card_row(db_path, 3)["queue"] == 3
    assert _card_row(db_path, 4)["queue"] == 1


def test_unbury_in_filtered_deck_reads_odue_for_the_learn_unit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    # Buried learn card parked in a filtered deck: due=position, odue=epoch.
    _insert_card(db_path, card_id=5, card_type=1, queue=-2, due=2, odue=1_700_000_300)

    store.unbury_cards()

    assert _card_row(db_path, 5)["queue"] == 1


def test_unbury_deck_scope_includes_children(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_000_000)

    _insert_card(db_path, card_id=101, did=10, card_type=2, queue=-2)
    _insert_card(db_path, card_id=102, did=11, card_type=0, queue=-3)
    _insert_card(db_path, card_id=103, did=12, card_type=3, queue=-2)

    result = store.unbury_cards(deck="Lang")
    assert result == {"unburied": 2, "deck": "Lang"}

    assert _card_row(db_path, 101)["queue"] == 2
    assert _card_row(db_path, 102)["queue"] == 0
    assert _card_row(db_path, 103)["queue"] == -2

    missing = store.unbury_cards(deck="Missing")
    assert missing == {"unburied": 0, "deck": "Missing"}


def test_reschedule_cards_sets_review_state_and_due(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_000_000)

    _insert_card(db_path, card_id=1, card_type=0, queue=0, due=2, ivl=0)
    _insert_card(db_path, card_id=5, card_type=1, queue=1, due=100, ivl=0)

    result = store.reschedule_cards(card_ids=[5, 1, 5, 999], days=3)
    assert result == {"rescheduled": 2, "card_ids": [1, 5, 999], "days": 3}

    expected_due = (1_000_000 // 86_400) + 3

    row1 = _card_row(db_path, 1)
    row5 = _card_row(db_path, 5)
    assert row1["type"] == 2
    assert row5["type"] == 2
    assert row1["queue"] == 2
    assert row5["queue"] == 2
    assert row1["due"] == expected_due
    assert row5["due"] == expected_due
    assert row1["ivl"] == 3
    assert row5["ivl"] == 3
    assert row1["mod"] == 1_000_000
    assert row5["mod"] == 1_000_000
    assert row1["usn"] == -1
    assert row5["usn"] == -1
    _assert_col_synced(db_path)


def test_reschedule_cards_negative_days_raises(tmp_path: Path) -> None:
    store, _db_path = _make_store(tmp_path)

    with pytest.raises(ValueError, match="days must be >= 0"):
        store.reschedule_cards(card_ids=[1], days=-1)


def test_reset_cards_reinitializes_state_and_assigns_new_due_sequence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_000_000)

    _insert_card(
        db_path,
        card_id=1,
        card_type=0,
        queue=0,
        due=20,
        ivl=0,
        factor=0,
        reps=0,
        lapses=0,
        left=0,
        data="{}",
    )
    _insert_card(
        db_path,
        card_id=10,
        card_type=2,
        queue=2,
        due=5,
        ivl=40,
        factor=2200,
        reps=9,
        lapses=2,
        left=1002,
        odue=3,
        odid=77,
        data='{"x":1}',
    )
    _insert_card(
        db_path,
        card_id=30,
        card_type=1,
        queue=1,
        due=900,
        ivl=3,
        factor=1800,
        reps=1,
        lapses=0,
        left=2002,
        odue=0,
        odid=0,
        data='{"y":2}',
    )

    result = store.reset_cards(card_ids=[30, 10, 30, 999, 0, -5])
    assert result == {"reset": 2, "card_ids": [10, 30, 999]}

    row10 = _card_row(db_path, 10)
    row30 = _card_row(db_path, 30)
    row1 = _card_row(db_path, 1)

    assert row10["type"] == 0
    assert row30["type"] == 0
    assert row10["queue"] == 0
    assert row30["queue"] == 0
    assert row10["due"] == 21
    assert row30["due"] == 22
    assert row10["ivl"] == 0
    assert row30["ivl"] == 0
    assert row10["factor"] == 0
    assert row30["factor"] == 0
    assert row10["reps"] == 0
    assert row30["reps"] == 0
    assert row10["lapses"] == 0
    assert row30["lapses"] == 0
    assert row10["left"] == 0
    assert row30["left"] == 0
    assert row10["odue"] == 0
    assert row30["odue"] == 0
    assert row10["odid"] == 0
    assert row30["odid"] == 0
    assert row10["data"] == "{}"
    assert row30["data"] == "{}"
    assert row10["mod"] == 1_000_000
    assert row30["mod"] == 1_000_000
    assert row10["usn"] == -1
    assert row30["usn"] == -1
    _assert_col_synced(db_path)

    # Existing new card remains unchanged.
    assert row1["queue"] == 0
    assert row1["due"] == 20


def test_card_mutator_noop_cases_return_empty_results(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)

    assert store.move_cards(card_ids=[], deck="Target") == {"moved": 0, "card_ids": []}
    assert store.set_card_flag(card_ids=[], flag=1) == {"updated": 0, "card_ids": []}
    assert store.bury_cards(card_ids=[]) == {"buried": 0, "card_ids": []}
    assert store.reschedule_cards(card_ids=[], days=2) == {"rescheduled": 0, "card_ids": []}
    assert store.reset_cards(card_ids=[]) == {"reset": 0, "card_ids": []}
    Collection(db_path).assert_untouched()  # no-ops write nothing


# --- filtered-deck awareness (#20) --------------------------------------------


def test_reset_cards_sends_a_filtered_deck_card_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Pre-fix, reset zeroed odid/odue but left did on the filtered deck: stranded."""
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    _insert_card(db_path, card_id=1, did=555, card_type=2, queue=2, due=-5, odid=10, odue=19_700)
    _insert_card(db_path, card_id=2, did=12, card_type=2, queue=2, due=19_900)

    store.reset_cards(card_ids=[1, 2])
    _assert_col_synced(db_path)

    home = _card_row(db_path, 1)
    assert (home["did"], home["odid"], home["odue"]) == (10, 0, 0)
    assert (home["type"], home["queue"]) == (0, 0)
    plain = _card_row(db_path, 2)
    assert plain["did"] == 12  # a card not on loan keeps its deck


def test_reschedule_cards_sends_a_filtered_deck_card_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_000_000)
    _insert_card(db_path, card_id=1, did=555, card_type=2, queue=2, due=-5, odid=10, odue=19_700)

    store.reschedule_cards(card_ids=[1], days=3)
    _assert_col_synced(db_path)

    row = _card_row(db_path, 1)
    assert (row["did"], row["odid"], row["odue"]) == (10, 0, 0)
    assert (row["type"], row["queue"], row["due"]) == (2, 2, 1_000_000 // 86400 + 3)


def test_move_cards_out_of_filtered_deck_restores_schedule(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    # Review card on loan: due is a filtered-deck position, odue the real due.
    _insert_card(db_path, card_id=1, did=555, card_type=2, queue=2, due=-5, odid=10, odue=19_700)
    # Intraday learn card on loan, suspended while there: queue must stay -1.
    _insert_card(
        db_path, card_id=2, did=555, card_type=1, queue=-1, due=-6, odid=10, odue=1_700_000_300
    )
    # Not on loan: only did changes.
    _insert_card(db_path, card_id=3, did=10, card_type=2, queue=2, due=19_800)

    store.move_cards(card_ids=[1, 2, 3], deck="Target")
    _assert_col_synced(db_path)

    review = _card_row(db_path, 1)
    assert (review["did"], review["due"], review["queue"], review["odid"]) == (2, 19_700, 2, 0)
    suspended = _card_row(db_path, 2)
    assert (suspended["did"], suspended["due"], suspended["queue"]) == (2, 1_700_000_300, -1)
    plain = _card_row(db_path, 3)
    assert (plain["did"], plain["due"], plain["queue"]) == (2, 19_800, 2)


def test_move_cards_ignores_a_stray_odue_on_a_card_not_on_loan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """rslib remove_from_filtered_deck_restoring_queue returns early when odid == 0."""
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    _insert_card(db_path, card_id=1, did=10, card_type=2, queue=2, due=19_800, odid=0, odue=19_700)

    store.move_cards(card_ids=[1], deck="Target")

    row = _card_row(db_path, 1)
    assert (row["did"], row["due"], row["queue"]) == (2, 19_800, 2)


def test_move_cards_into_a_filtered_deck_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """rslib FilteredDeckError::CanNotMoveCardsInto."""
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    _insert_card(db_path, card_id=1, did=10)

    with pytest.raises(ValueError, match="filtered deck"):
        store.move_cards(card_ids=[1], deck="Cram")
    Collection(db_path).assert_untouched()

    assert _card_row(db_path, 1)["did"] == 10
