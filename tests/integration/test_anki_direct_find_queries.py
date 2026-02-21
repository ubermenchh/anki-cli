from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import anki_cli.db.anki_direct as direct_mod
from anki_cli.db.anki_direct import AnkiDirectReadStore


def _make_store(
    tmp_path: Path,
    *,
    decks: list[tuple[int, str]],
    notes: list[tuple[int, str, str, int]],
    cards: list[tuple[int, int, int, int, int]],
    col_crt: int = 0,
) -> AnkiDirectReadStore:
    db_path = tmp_path / "collection.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE col (
            crt INTEGER NOT NULL
        );

        CREATE TABLE decks (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        );

        CREATE TABLE notes (
            id INTEGER PRIMARY KEY,
            tags TEXT NOT NULL,
            flds TEXT NOT NULL,
            mod INTEGER NOT NULL
        );

        CREATE TABLE cards (
            id INTEGER PRIMARY KEY,
            nid INTEGER NOT NULL,
            did INTEGER NOT NULL,
            queue INTEGER NOT NULL,
            due INTEGER NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO col (crt) VALUES (?)", (col_crt,))
    conn.executemany("INSERT INTO decks (id, name) VALUES (?, ?)", decks)
    conn.executemany("INSERT INTO notes (id, tags, flds, mod) VALUES (?, ?, ?, ?)", notes)
    conn.executemany("INSERT INTO cards (id, nid, did, queue, due) VALUES (?, ?, ?, ?, ?)", cards)
    conn.commit()
    conn.close()

    return AnkiDirectReadStore(db_path)


def _seed_store(tmp_path: Path) -> AnkiDirectReadStore:
    return _make_store(
        tmp_path,
        decks=[
            (1, "Default"),
            (2, "Lang::Spanish"),
            (3, "Lang::French"),
            (4, "Archive"),
        ],
        notes=[
            (101, " foo spanish ", "hola\x1fhello", 100),
            (102, " foo french ", "bonjour\x1fhello", 200),
            (103, " bar ", "ciao\x1fhello", 300),
            (104, " suspended ", "hold\x1fcard", 400),
        ],
        cards=[
            (1001, 101, 2, 0, 0),         # new (always due)
            (1002, 101, 2, 1, 999_999),   # learn due
            (1003, 101, 2, 1, 1_000_100), # learn not due
            (1004, 102, 3, 3, 1_000_000), # relearn due
            (1005, 102, 3, 2, 11),        # review due (with now=1_000_000, crt=0)
            (1006, 103, 1, 2, 12),        # review not due
            (1007, 104, 4, -1, 0),        # suspended
        ],
        col_crt=0,
    )


def test_find_note_ids_by_tag_and_exact_deck(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)

    assert store.find_note_ids("tag:foo") == [101, 102]
    assert store.find_note_ids('tag:foo deck:"Lang::Spanish"') == [101]


def test_find_note_ids_supports_deck_wildcards_and_text_search(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)

    assert store.find_note_ids("deck:Lang::* tag:foo") == [101, 102]
    assert store.find_note_ids("bonjour") == [102]
    assert store.find_note_ids("nid:103") == [103]


def test_find_card_ids_basic_token_filters(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)

    assert store.find_card_ids("cid:1005") == [1005]
    assert store.find_card_ids("nid:102") == [1004, 1005]
    assert store.find_card_ids("tag:foo deck:Lang::Spanish") == [1001, 1002, 1003]


def test_find_card_ids_is_filters_and_due_logic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_000_000)
    store = _seed_store(tmp_path)

    assert store.find_card_ids("is:new") == [1001]
    assert store.find_card_ids("is:learn") == [1002, 1003, 1004]
    assert store.find_card_ids("is:review") == [1005, 1006]
    assert store.find_card_ids("is:suspended") == [1007]
    assert store.find_card_ids("is:due") == [1001, 1002, 1004, 1005]


def test_find_card_ids_combines_due_and_deck_filters(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_000_000)
    store = _seed_store(tmp_path)

    assert store.find_card_ids("is:due deck:Lang::French") == [1004, 1005]
    assert store.find_card_ids("is:due deck:Default") == []