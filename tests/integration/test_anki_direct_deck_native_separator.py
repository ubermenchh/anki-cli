"""Hierarchical deck names are stored with ``\\x1f`` between components (rslib
``NativeDeckName``) and shown/typed with ``::`` (#75). The store converts at the
edge: reads return ``::``, lookups and writes accept ``::`` and store ``\\x1f``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from anki_cli.db import lock
from anki_cli.db.deck_names import deck_human_to_native, deck_native_to_human
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


def test_deck_config_and_next_due_accept_the_human_form(tmp_path: Path) -> None:
    store, col = _make_store(tmp_path)
    col.insert_deck_config()

    assert store.get_deck_config("GPU::CUDA")["deck"] == "GPU::CUDA"
    store.set_deck_config("GPU::CUDA", {"new_per_day": 7})
    assert store.get_deck_config("GPU::CUDA")["config"]["new_per_day"] == 7

    col.insert_card(id=1001, nid=100, did=21, type=2, queue=2, due=0, ivl=5)
    nxt = store._get_next_due_for_deck("GPU::CUDA")
    assert nxt is not None
    assert nxt["queue"] == 2
    assert store._get_next_due_for_deck("GPU::Nope") is None


def test_subtree_lookups_accept_the_human_form(tmp_path: Path) -> None:
    store, col = _make_store(tmp_path)
    col.insert_deck(id=22, name="GPU::CUDA::Sub")
    col.insert_deck(id=23, name="GPUX")

    with store._connect() as conn:
        found = store._deck_subtree(conn, "gpu::cuda")
    assert found is not None
    assert found.name == "GPU\x1fCUDA"
    assert sorted(int(r["id"]) for r in found.rows) == [21, 22]  # not GPUX, not the parent

    assert store.rename_deck(old_name="GPU::CUDA", new_name="GPU::Tensor")["items"] == [
        {"id": 21, "from": "GPU::CUDA", "to": "GPU::Tensor"},
        {"id": 22, "from": "GPU::CUDA::Sub", "to": "GPU::Tensor::Sub"},
    ]
    assert store.delete_deck("GPU::Tensor")["deleted_decks"] == 2
    assert {20, 23} <= set(col.ids("decks"))  # parent and sibling survive


def test_deck_search_covers_children_but_not_prefix_siblings(tmp_path: Path) -> None:
    store, col = _make_store(tmp_path)
    col.insert_deck(id=22, name="GPUX")
    col.insert_card(id=1002, nid=100, did=22)
    col.insert_deck(id=23, name="GPU::CUDA::Sub")
    col.insert_card(id=1003, nid=100, did=23)

    assert 1002 not in store.find_cards("deck:GPU")
    assert sorted(store.find_cards("deck:GPU::CUDA")) == [1000, 1003]
    assert sorted(store.find_cards("deck:GPU::*")) == [1000, 1003]


def test_component_ending_in_a_colon_round_trips(tmp_path: Path) -> None:
    """``Ch 1:`` + ``Verbs`` reads ``Ch 1:::Verbs``; a blind replace would look
    for ``Ch 1\\x1f:Verbs``."""
    store, col = _make_store(tmp_path)
    col.insert_deck(id=30, name="Ch 1:")
    col.insert_deck(id=31, name="x")
    col.execute("UPDATE decks SET name = ? WHERE id = 31", ("Ch 1:\x1fVerbs",))
    col.insert_card(id=1004, nid=100, did=31)

    assert "Ch 1:::Verbs" in [d["name"] for d in store.get_decks()]
    assert store.get_deck("Ch 1:::Verbs")["id"] == 31
    assert store.move_cards([1000], "Ch 1:::Verbs")["moved"] == 1
    renamed = store.rename_deck(old_name="Ch 1:::Verbs", new_name="Words::Verbs")
    assert renamed["renamed_decks"] == 1
    assert col.row("decks", 31)["name"] == "Words\x1fVerbs"


# ---- legacy '::' rows written by older versions ---------------------------------------


def _legacy_store(
    tmp_path: Path, *, names: dict[int, str] | None = None, writable: bool = True
) -> tuple[AnkiDirectStore, Collection]:
    """A collection an older anki-cli wrote: ``::`` stored verbatim, with the
    ``usn`` of a synced row (263) so a migration that forgets ``usn = -1`` shows."""
    col = new_collection(tmp_path / "collection.anki2", seed=False)
    col.insert_notetype(id=10, name="Basic", fields=["Front", "Back"])
    col.insert_deck(id=1, name="Default")
    col.insert_deck(id=20, name="GPU")
    col.insert_note(id=100, fields=["f", "b"], mid=10)
    for did, name in (names or {21: "GPU::Metal", 22: "GPU::CUDA"}).items():
        col.insert_deck(id=did, name="x")
        col.execute(
            "UPDATE decks SET name = ?, usn = 263, mtime_secs = 1 WHERE id = ?", (name, did)
        )
    return col.store(writable=writable), col


def test_legacy_rows_are_normalised_before_the_first_read(tmp_path: Path) -> None:
    store, col = _legacy_store(tmp_path)
    col.insert_card(id=1000, nid=100, did=21)

    # A read, with no write in sight: lookups must already see the native name.
    assert store.find_cards("deck:GPU::Metal") == [1000]
    assert store.get_deck("GPU::Metal")["id"] == 21
    assert col.row("decks", 21)["name"] == "GPU\x1fMetal"
    assert col.row("decks", 22)["name"] == "GPU\x1fCUDA"
    assert col.row("decks", 21)["usn"] == -1  # marked for sync, unlike the synced 263
    assert col.row("decks", 21)["mtime_secs"] > 1


def test_normalising_does_nothing_without_legacy_rows(tmp_path: Path) -> None:
    store, col = _make_store(tmp_path)
    before = col.row("decks", 21)

    store.get_decks()
    store.write_deck(name="Other")

    assert col.row("decks", 21) == before
    assert store.deck_name_conflicts == []


def test_legacy_rows_with_a_native_twin_are_reported_not_rewritten(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store, col = _legacy_store(tmp_path)
    col.insert_deck(id=23, name="GPU::Metal")  # native twin already exists

    with caplog.at_level(logging.WARNING):
        store.get_decks()

    assert col.row("decks", 21)["name"] == "GPU::Metal"
    assert col.row("decks", 21)["usn"] == 263  # untouched
    assert col.row("decks", 23)["name"] == "GPU\x1fMetal"
    assert col.row("decks", 22)["name"] == "GPU\x1fCUDA"  # the others still migrate
    assert store.deck_name_conflicts == ["GPU::Metal"]
    assert "GPU::Metal" in caplog.text


@pytest.mark.parametrize("bad", ["A::::B", "A::", "::A"])
def test_legacy_names_with_empty_components_are_left_alone(tmp_path: Path, bad: str) -> None:
    store, col = _legacy_store(tmp_path, names={21: bad, 22: "GPU::CUDA"})

    store.get_decks()

    assert col.row("decks", 21)["name"] == bad
    assert col.row("decks", 22)["name"] == "GPU\x1fCUDA"
    assert store.deck_name_conflicts == [bad]


def test_two_level_legacy_name_without_its_parent_rows(tmp_path: Path) -> None:
    store, col = _legacy_store(tmp_path, names={21: "A::B::C"})

    store.get_decks()

    assert col.row("decks", 21)["name"] == "A\x1fB\x1fC"


def test_legacy_rows_are_left_alone_while_anki_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store, col = _legacy_store(tmp_path, writable=False)
    monkeypatch.setattr(lock, "anki_process_running", lambda: True)

    with caplog.at_level(logging.WARNING):
        decks = store.get_decks()

    assert col.row("decks", 21)["name"] == "GPU::Metal"  # a read never forces a write
    assert "GPU::Metal" in [d["name"] for d in decks]
    assert "cannot be written" in caplog.text


def test_codec_round_trips() -> None:
    assert deck_human_to_native("A::B::C") == "A\x1fB\x1fC"
    assert deck_native_to_human("A\x1fB\x1fC") == "A::B::C"
    assert deck_human_to_native("Flat") == "Flat"
    assert deck_human_to_native("") == ""
    assert deck_native_to_human(deck_human_to_native("A::B")) == "A::B"
