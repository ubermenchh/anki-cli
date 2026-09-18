"""The shared fixture's DDL is Anki's, and its defaults decode the way the
store expects (#37). If this fails, every other integration test is running
against a schema that is not Anki's."""

from __future__ import annotations

import pytest

from anki_cli.db.anki_direct import MIN_SUPPORTED_SCHEMA_VERSION
from tests.anki_schema import SCHEMA_VERSION, TABLES
from tests.conftest import (
    COL_BASE_MOD_MS,
    DEFAULT_DECK_CONFIG_ID,
    Collection,
)

# Every column of every table Anki Desktop writes, per ``sqlite_master`` of a
# schema-18 collection. The store may read any of these; a fixture that lacks
# one hides regressions in whatever the store does with it.
EXPECTED_COLUMNS = {
    "cards": ["id", "nid", "did", "ord", "mod", "usn", "type", "queue", "due", "ivl", "factor",
              "reps", "lapses", "left", "odue", "odid", "flags", "data"],
    "col": ["id", "crt", "mod", "scm", "ver", "dty", "usn", "ls", "conf", "models", "decks",
            "dconf", "tags"],
    "config": ["KEY", "usn", "mtime_secs", "val"],
    "deck_config": ["id", "name", "mtime_secs", "usn", "config"],
    "decks": ["id", "name", "mtime_secs", "usn", "common", "kind"],
    "fields": ["ntid", "ord", "name", "config"],
    "graves": ["oid", "type", "usn"],
    "notes": ["id", "guid", "mid", "mod", "usn", "tags", "flds", "sfld", "csum", "flags", "data"],
    "notetypes": ["id", "name", "mtime_secs", "usn", "config"],
    "revlog": ["id", "cid", "usn", "ease", "ivl", "lastIvl", "factor", "time", "type"],
    "tags": ["tag", "usn", "collapsed", "config"],
    "templates": ["ntid", "ord", "name", "mtime_secs", "usn", "config"],
}


def test_fixture_has_every_anki_table_and_column(bare_collection: Collection) -> None:
    conn = bare_collection.connect()
    try:
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        assert set(TABLES) <= tables
        for table, columns in EXPECTED_COLUMNS.items():
            info = conn.execute(f"PRAGMA table_info({table})").fetchall()
            assert [r["name"] for r in info] == columns, table
            # Anki declares every column NOT NULL except tags.config.
            nullable = [r["name"] for r in info if not r["notnull"] and r["pk"] == 0]
            assert nullable == (["config"] if table == "tags" else []), (table, nullable)
        indexes = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
        )}
        assert {"ix_cards_nid", "ix_cards_sched", "ix_notes_csum", "idx_decks_name",
                "ix_revlog_cid"} <= indexes
    finally:
        conn.close()


def test_fixture_col_row_passes_the_store_schema_guard(bare_collection: Collection) -> None:
    col = bare_collection.col()
    assert col["ver"] == SCHEMA_VERSION >= MIN_SUPPORTED_SCHEMA_VERSION
    assert col["mod"] == COL_BASE_MOD_MS
    bare_collection.store(writable=False)  # __init__ runs _check_schema_version


def test_seeded_defaults_decode_like_anki(collection: Collection) -> None:
    """The protobuf blobs the builders write are the ones the store expects:
    a *normal* deck (``b""`` would be "unknown"), real learning steps (``[]``
    would mean "no steps", #57), a normal notetype with two fields and a
    template that actually renders."""
    store = collection.store(writable=False)

    (deck,) = store.get_decks()
    assert (deck["id"], deck["name"], deck["kind"]) == (1, "Default", "normal")
    assert deck["config_id"] == DEFAULT_DECK_CONFIG_ID

    cfg = store.get_deck_config("Default")["config"]
    assert cfg["learn_steps"] == [1.0, 10.0] and cfg["relearn_steps"] == [10.0]
    assert cfg["desired_retention"] == pytest.approx(0.9)  # float32 in the proto

    nt = store.get_notetype("Basic")
    assert (nt["id"], nt["kind"], nt["fields"]) == (10, "normal", ["Front", "Back"])
    assert nt["templates"]["Card 1"]["Front"] == "{{Front}}"

    # add_note generates exactly one card from the seeded template.
    nid = store.add_note(deck="Default", notetype="Basic", fields={"Front": "Q", "Back": "A"},
                         tags=None, allow_duplicate=False)
    assert len(collection.rows("cards", "nid = ?", (nid,))) == 1


def test_filtered_deck_builder_decodes_as_filtered(collection: Collection) -> None:
    collection.insert_filtered_deck(id=555, name="Cram")
    kinds = {d["name"]: d["kind"] for d in collection.store(writable=False).get_decks()}
    assert kinds == {"Default": "normal", "Cram": "filtered"}


def test_assert_synced_catches_each_missing_bookkeeping_step(collection: Collection) -> None:
    """Mutation check on the helper itself: a row the store wrote passes; a row
    that skipped any one of the three sync marks fails."""
    store = collection.store()
    nid = store.add_note(deck="Default", notetype="Basic", fields={"Front": "Q", "Back": "A"},
                         tags=None, allow_duplicate=False)
    collection.assert_synced("notes", nid)

    # Forge each defect in turn.
    collection.execute("UPDATE notes SET usn = 0 WHERE id = ?", (nid,))
    with pytest.raises(AssertionError, match="usn"):
        collection.assert_synced("notes", nid)
    collection.execute("UPDATE notes SET usn = -1, mod = 1 WHERE id = ?", (nid,))
    with pytest.raises(AssertionError, match="did not move"):
        collection.assert_synced("notes", nid)
    collection.execute("UPDATE notes SET mod = 999 WHERE id = ?", (nid,))
    collection.execute("UPDATE col SET mod = ?", (COL_BASE_MOD_MS,))
    with pytest.raises(AssertionError, match=r"col\.mod"):
        collection.assert_synced("notes", nid)
