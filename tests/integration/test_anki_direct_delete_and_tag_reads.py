from __future__ import annotations

from pathlib import Path

import pytest

from anki_cli.db.store import AnkiDirectStore
from tests.conftest import COL_BASE_MOD_MS, Collection, new_collection


def _make_store(tmp_path: Path) -> tuple[AnkiDirectStore, Path]:
    col = new_collection(tmp_path / "collection.anki2", seed=False)
    return col.store(writable=False), col.db_path


def _insert_note(db_path: Path, *, note_id: int, tags: str) -> None:
    Collection(db_path).insert_note(id=note_id, fields=["Q", "A"], tags=tags)


def _insert_card(db_path: Path, *, card_id: int) -> None:
    Collection(db_path).insert_card(id=card_id, nid=1000)


def _card_ids(db_path: Path) -> list[int]:
    return Collection(db_path).ids("cards")


def _grave_rows(db_path: Path) -> list[tuple[int, int, int]]:
    return Collection(db_path).graves()


def test_delete_card_non_positive_returns_deleted_false(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)
    _insert_card(db_path, card_id=10)

    assert store.delete_card(0) == {"card_id": 0, "deleted": False}
    assert store.delete_card(-7) == {"card_id": -7, "deleted": False}

    assert _card_ids(db_path) == [10]
    assert _grave_rows(db_path) == []


def test_delete_card_missing_returns_deleted_false(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    _insert_card(db_path, card_id=10)

    assert store.delete_card(999) == {"card_id": 999, "deleted": False}
    assert _card_ids(db_path) == [10]
    assert _grave_rows(db_path) == []
    Collection(db_path).assert_untouched()  # nothing to delete, nothing to sync


def test_delete_card_existing_deletes_card_and_inserts_grave(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    _insert_card(db_path, card_id=10)
    _insert_card(db_path, card_id=20)

    assert store.delete_card(10) == {"card_id": 10, "deleted": True}
    assert _card_ids(db_path) == [20]
    assert _grave_rows(db_path) == [(10, 0, -1)]
    assert Collection(db_path).col()["mod"] > COL_BASE_MOD_MS  # sync notices the grave


def test_delete_card_second_call_is_noop_and_grave_not_duplicated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    _insert_card(db_path, card_id=42)

    assert store.delete_card(42) == {"card_id": 42, "deleted": True}
    assert store.delete_card(42) == {"card_id": 42, "deleted": False}
    assert _card_ids(db_path) == []
    assert _grave_rows(db_path) == [(42, 0, -1)]


def test_get_tags_returns_unique_sorted_case_insensitive(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)

    _insert_note(db_path, note_id=1, tags=" Zulu alpha ")
    _insert_note(db_path, note_id=2, tags=" beta ")
    _insert_note(db_path, note_id=3, tags=" alpha ")

    assert store.get_tags() == ["alpha", "beta", "Zulu"]


def test_get_tag_counts_counts_occurrences(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)

    _insert_note(db_path, note_id=1, tags=" alpha beta ")
    _insert_note(db_path, note_id=2, tags=" beta gamma ")
    _insert_note(db_path, note_id=3, tags=" gamma ")

    assert store.get_tag_counts() == [
        {"tag": "alpha", "count": 1},
        {"tag": "beta", "count": 2},
        {"tag": "gamma", "count": 2},
    ]


def test_get_tag_counts_treats_case_variants_as_distinct(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)

    _insert_note(db_path, note_id=1, tags=" Foo ")
    _insert_note(db_path, note_id=2, tags=" foo Foo ")

    counts = {item["tag"]: item["count"] for item in store.get_tag_counts()}
    assert counts == {"Foo": 2, "foo": 1}


def test_get_tags_and_counts_empty_db_return_empty(tmp_path: Path) -> None:
    store, _db_path = _make_store(tmp_path)

    assert store.get_tags() == []
    assert store.get_tag_counts() == []
