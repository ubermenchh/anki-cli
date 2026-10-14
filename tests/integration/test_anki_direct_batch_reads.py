"""``get_cards`` / ``get_notes`` fetch many rows over one connection (#35)."""

from __future__ import annotations

from pathlib import Path

import pytest

from anki_cli.db.connection import SQL_IN_CHUNK
from anki_cli.db.store import AnkiDirectStore
from tests.conftest import new_collection


def _store(tmp_path: Path, n: int) -> AnkiDirectStore:
    col = new_collection(tmp_path / "collection.anki2", seed=False)
    col.insert_notetype(id=10, name="Basic", fields=["Front", "Back"])
    col.insert_deck(id=1, name="Default")
    for i in range(1, n + 1):
        col.insert_note(id=100 + i, fields=[f"f{i}", f"b{i}"], mid=10)
        col.insert_card(id=1000 + i, nid=100 + i, did=1)
    return col.store()


def test_get_cards_matches_get_card_in_request_order(tmp_path: Path) -> None:
    store = _store(tmp_path, 3)
    got = store.get_cards([1003, 1001, 1002])
    assert [c["cardId"] for c in got] == [1003, 1001, 1002]
    assert got[1] == store.get_card(1001)


def test_get_cards_skips_missing_dedupes_and_accepts_empty(tmp_path: Path) -> None:
    store = _store(tmp_path, 2)
    assert [c["cardId"] for c in store.get_cards([1001, 9999, 1001, 1002])] == [1001, 1002]
    assert store.get_cards([]) == []
    with pytest.raises(LookupError):
        store.get_card(9999)


def test_get_notes_matches_get_note_and_skips_missing(tmp_path: Path) -> None:
    store = _store(tmp_path, 3)
    got = store.get_notes([103, 555, 101])
    assert [n["id"] for n in got] == [103, 101]
    assert got[1] == store.get_note(101)
    with pytest.raises(LookupError):
        store.get_note(555)


def test_batches_larger_than_one_sql_chunk(tmp_path: Path) -> None:
    n = SQL_IN_CHUNK * 2 + 7
    store = _store(tmp_path, n)
    ids = [1000 + i for i in range(n, 0, -1)]
    assert [c["cardId"] for c in store.get_cards(ids)] == ids
    assert len(store.get_notes([100 + i for i in range(1, n + 1)])) == n


def test_get_cards_opens_one_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path, 20)
    opened = 0
    real = store._connect

    def counting():
        nonlocal opened
        opened += 1
        return real()

    monkeypatch.setattr(store, "_connect", counting)
    store.get_cards([1000 + i for i in range(1, 21)])
    assert opened == 1
