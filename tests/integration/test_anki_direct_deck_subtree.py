"""Deck-subtree matching (#73): "a deck and its children" must mean the same
thing everywhere and must treat deck names as literal text.

Anki matches the parent case-insensitively (``decks.name`` is ``COLLATE
unicase``) and children by exact ``"<parent>::"`` prefix. The old code built
the scope with ``LIKE`` and no ``ESCAPE``, so ``_``/``%`` in a name were
wildcards (``delete_deck("A_B")`` also deleted ``AXB::child``), ``LIKE``'s
ASCII-only folding missed ``École`` for ``école``, and ``_deck_filter``'s
explicit ``COLLATE NOCASE`` downgraded the column's unicase to ASCII.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anki_cli.db.store import AnkiDirectStore
from tests.conftest import Collection, new_collection

# id -> name. A_B and AXB are LIKE-wildcard neighbours; 100%::x tests '%';
# École tests non-ASCII folding; Base/Basement tests that a prefix without
# '::' is not a child.
DECKS = {
    1: "Default",
    10: "A_B",
    11: "A_B::child",
    20: "AXB",
    21: "AXB::child",
    30: "100%",
    31: "100%::x",
    32: "100x",
    33: "100x::y",
    40: "École",
    41: "École::Verbs",
    50: "Base",
    51: "Basement",
    52: "Base::Child",
    60: "Lang",
    61: "lang::De",  # parent segment in another case: still Lang's child (unicase)
}


def _make_store(tmp_path: Path) -> tuple[AnkiDirectStore, Collection]:
    col = new_collection(tmp_path / "collection.anki2", seed=False)
    col.insert_notetype(id=10, name="Basic", fields=["Front", "Back"])
    for did, name in DECKS.items():
        col.insert_deck(id=did, name=name)
    return col.store(writable=True), col


def _subtree_ids(store: AnkiDirectStore, name: str) -> list[int] | None:
    with store._connect() as conn:
        found = store._deck_subtree(conn, name)
    if found is None:
        return None
    return sorted(int(row["id"]) for row in found.rows)


# ---- the helper -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected_ids"),
    [
        ("A_B", [10, 11]),  # '_' is literal: AXB::child is not a child of A_B
        ("AXB", [20, 21]),
        ("100%", [30, 31]),  # '%' is literal: 100x::y is not a child
        ("école", [40, 41]),  # unicase parent match, children follow the stored name
        ("ÉCOLE::verbs", [41]),
        ("Base", [50, 52]),  # Basement is a sibling, not a child
        ("  base  ", [50, 52]),  # whitespace stripped, case folded
        ("a_b::CHILD", [11]),
        ("Lang", [60, 61]),
        ("LANG::de", [61]),
    ],
)
def test_deck_subtree_matches_literal_prefix_case_insensitively(
    tmp_path: Path, name: str, expected_ids: list[int]
) -> None:
    store, _col = _make_store(tmp_path)
    assert _subtree_ids(store, name) == expected_ids


def test_deck_subtree_reports_canonical_name_and_parents_first(tmp_path: Path) -> None:
    store, _col = _make_store(tmp_path)
    with store._connect() as conn:
        found = store._deck_subtree(conn, "a_b")
    assert found is not None
    assert found.name == "A_B"
    assert [str(r["name"]) for r in found.rows] == ["A_B", "A_B::child"]


@pytest.mark.parametrize("name", ["", "   ", "Missing", "A_B::", "A_", "Bas"])
def test_deck_subtree_none_when_no_such_deck(tmp_path: Path, name: str) -> None:
    store, _col = _make_store(tmp_path)
    assert _subtree_ids(store, name) is None


# ---- the four callers ---------------------------------------------------------------


def test_delete_deck_with_like_wildcard_in_name_leaves_neighbours_alone(
    tmp_path: Path,
) -> None:
    store, col = _make_store(tmp_path)
    col.insert_note(id=1000, fields=["q", "a"])
    col.insert_card(id=2000, nid=1000, did=11)  # A_B::child
    col.insert_card(id=2001, nid=1000, did=21)  # AXB::child

    result = store.delete_deck("A_B")

    assert result["deleted_decks"] == 2
    assert result["deleted_cards"] == 1
    assert col.ids("cards") == [2001]
    assert 20 in col.ids("decks") and 21 in col.ids("decks")
    assert set(col.ids("decks")) & {10, 11} == set()


def test_delete_deck_percent_in_name(tmp_path: Path) -> None:
    store, col = _make_store(tmp_path)

    assert store.delete_deck("100%")["deleted_decks"] == 2
    assert {32, 33} <= set(col.ids("decks"))


def test_delete_deck_matches_parent_case_insensitively(tmp_path: Path) -> None:
    store, col = _make_store(tmp_path)

    result = store.delete_deck("école")

    assert result["deleted_decks"] == 2
    assert not {40, 41} & set(col.ids("decks"))


def test_rename_deck_scopes_by_literal_prefix_and_canonical_name(tmp_path: Path) -> None:
    store, col = _make_store(tmp_path)

    result = store.rename_deck(old_name="a_b", new_name="Renamed")

    assert result["renamed_decks"] == 2
    names = {did: str(col.row("decks", did)["name"]) for did in (10, 11, 20, 21)}
    # Suffix is taken from the stored name, so a case-folded source still
    # yields the right child path.
    assert names == {10: "Renamed", 11: "Renamed::child", 20: "AXB", 21: "AXB::child"}


def test_rename_deck_target_conflict_probe_is_literal_too(tmp_path: Path) -> None:
    """Renaming onto ``a_b`` must be refused because ``A_B`` exists (unicase), but
    renaming onto ``AX_`` must not be refused just because ``AX_::%`` would
    LIKE-match ``AXB::child``."""
    store, col = _make_store(tmp_path)

    with pytest.raises(ValueError, match="Target deck path already exists"):
        store.rename_deck(old_name="Base", new_name="a_b")

    assert store.rename_deck(old_name="Base", new_name="AX_")["renamed_decks"] == 2
    assert str(col.row("decks", 52)["name"]) == "AX_::Child"


def test_rename_deck_case_only_rename_of_a_subtree(tmp_path: Path) -> None:
    """``école`` -> ``ÉCOLE`` is a real rename (Anki shows the new spelling): the
    source is found unicase, its children too (``LIKE`` is ASCII-only and used
    to miss them), and the target probe must recognise the deck it finds as the
    one being renamed."""
    store, col = _make_store(tmp_path)

    result = store.rename_deck(old_name="école", new_name="ÉCOLE")

    assert result["renamed_decks"] == 2
    assert str(col.row("decks", 40)["name"]) == "ÉCOLE"
    assert str(col.row("decks", 41)["name"]) == "ÉCOLE::Verbs"


def test_unbury_deck_scope_is_literal_and_case_insensitive(tmp_path: Path) -> None:
    store, col = _make_store(tmp_path)
    col.insert_note(id=1000, fields=["q", "a"])
    col.insert_card(id=2000, nid=1000, did=11, type=2, queue=-2)  # A_B::child
    col.insert_card(id=2001, nid=1000, did=21, type=2, queue=-2)  # AXB::child
    col.insert_card(id=2002, nid=1000, did=41, type=2, queue=-3)  # École::Verbs

    assert store.unbury_cards(deck="a_b") == {"unburied": 1, "deck": "a_b"}
    assert int(col.row("cards", 2000)["queue"]) == 2
    assert int(col.row("cards", 2001)["queue"]) == -2

    assert store.unbury_cards(deck="école")["unburied"] == 1
    assert int(col.row("cards", 2002)["queue"]) == 2


def test_deck_filter_uses_unicase_not_ascii_nocase(tmp_path: Path) -> None:
    """``--deck école`` (via ``_deck_filter``) must find ``École``: the column is
    ``COLLATE unicase`` and an explicit ``COLLATE NOCASE`` is ASCII-only."""
    store, col = _make_store(tmp_path)
    col.insert_note(id=1000, fields=["q", "a"])
    col.insert_card(id=2000, nid=1000, did=41, type=0, queue=0, due=1)
    col.insert_card(id=2001, nid=1000, did=21, type=0, queue=0, due=2)  # AXB::child

    assert store.get_due_counts(deck="école")["new"] == 1
    assert store.get_due_counts(deck="A_B")["new"] == 0  # not AXB::child
    assert store.get_due_counts(deck="axb")["new"] == 1
