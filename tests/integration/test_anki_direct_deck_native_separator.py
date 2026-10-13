"""Hierarchical deck names are stored with ``\\x1f`` between components (rslib
``NativeDeckName``) and shown/typed with ``::`` (#75). The store converts at the
edge: reads return ``::``, lookups and writes accept ``::`` and store ``\\x1f``.
"""

from __future__ import annotations

from pathlib import Path

from anki_cli.db.store import AnkiDirectStore
from tests.conftest import Collection, new_collection


def _make_store(tmp_path: Path) -> tuple[AnkiDirectStore, Collection]:
    col = new_collection(tmp_path / "collection.anki2", seed=False)
    col.insert_notetype(id=10, name="Basic", fields=["Front", "Back"])
    col.insert_deck(id=1, name="Default")
    col.insert_deck(id=20, name="GPU")
    col.insert_deck(id=21, name="GPU::CUDA")  # fixture writes the native form
    col.insert_note(id=100, fields=["f", "b"], mid=10)
    col.insert_card(id=1000, nid=100, did=21)
    return col.store(writable=True), col


def test_fixture_stores_the_native_separator(tmp_path: Path) -> None:
    _store, col = _make_store(tmp_path)
    assert col.row("decks", 21)["name"] == "GPU\x1fCUDA"


def test_get_decks_and_get_card_report_the_human_form(tmp_path: Path) -> None:
    store, _col = _make_store(tmp_path)
    assert "GPU::CUDA" in [d["name"] for d in store.get_decks()]
    assert store.get_card(1000)["deckName"] == "GPU::CUDA"


def test_deck_lookups_accept_the_human_form_for_an_anki_created_child(tmp_path: Path) -> None:
    store, col = _make_store(tmp_path)
    col.insert_card(id=1001, nid=100, did=20)

    assert store.find_cards("deck:GPU::CUDA") == [1000]
    assert sorted(store.find_cards("deck:GPU")) == [1000, 1001]  # parent covers the child
    assert store.get_deck("GPU::CUDA")["id"] == 21

    assert store.move_cards([1001], "GPU::CUDA")["moved"] == 1
    assert col.row("cards", 1001)["did"] == 21


def test_write_deck_stores_native_and_does_not_create_a_top_level_deck(tmp_path: Path) -> None:
    store, col = _make_store(tmp_path)

    created = store.write_deck(name="GPU::Metal")

    assert col.row("decks", int(created["id"]))["name"] == "GPU\x1fMetal"
    assert not [n for n in (str(r["name"]) for r in col.rows("decks")) if "::" in n]


def test_rename_deck_stores_native_and_reports_human(tmp_path: Path) -> None:
    store, col = _make_store(tmp_path)

    result = store.rename_deck(old_name="GPU", new_name="Compute::GPU")

    assert col.row("decks", 20)["name"] == "Compute\x1fGPU"
    assert col.row("decks", 21)["name"] == "Compute\x1fGPU\x1fCUDA"
    assert result["items"] == [
        {"id": 20, "from": "GPU", "to": "Compute::GPU"},
        {"id": 21, "from": "GPU::CUDA", "to": "Compute::GPU::CUDA"},
    ]


def test_delete_deck_removes_the_native_children(tmp_path: Path) -> None:
    store, col = _make_store(tmp_path)

    assert store.delete_deck("GPU")["deleted_decks"] == 2
    assert not {20, 21} & set(col.ids("decks"))


def _legacy_store(tmp_path: Path) -> tuple[AnkiDirectStore, Collection]:
    """A collection an older anki-cli wrote: ``::`` stored verbatim."""
    col = new_collection(tmp_path / "collection.anki2", seed=False)
    col.insert_notetype(id=10, name="Basic", fields=["Front", "Back"])
    col.insert_deck(id=1, name="Default")
    col.insert_deck(id=20, name="GPU")
    for did, name in ((21, "GPU::Metal"), (22, "GPU::CUDA")):
        col.insert_deck(id=did, name="x")
        col.execute("UPDATE decks SET name = ?, usn = 263 WHERE id = ?", (name, did))
    return col.store(writable=True), col


def test_a_write_normalises_legacy_double_colon_rows(tmp_path: Path) -> None:
    store, col = _legacy_store(tmp_path)
    assert col.row("decks", 21)["name"] == "GPU::Metal"

    store.write_deck(name="Anything")  # any write transaction

    assert col.row("decks", 21)["name"] == "GPU\x1fMetal"
    assert col.row("decks", 22)["name"] == "GPU\x1fCUDA"
    assert col.row("decks", 21)["usn"] == -1  # marked for sync
    assert store.get_deck("GPU::Metal")["id"] == 21


def test_legacy_row_is_left_alone_when_the_native_name_is_taken(tmp_path: Path) -> None:
    store, col = _legacy_store(tmp_path)
    col.insert_deck(id=23, name="GPU::Metal")  # native twin already exists

    store.write_deck(name="Anything")

    assert col.row("decks", 21)["name"] == "GPU::Metal"
    assert col.row("decks", 23)["name"] == "GPU\x1fMetal"
