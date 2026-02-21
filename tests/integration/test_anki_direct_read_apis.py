from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import anki_cli.db.anki_direct as direct_mod
from anki_cli.db.anki_direct import AnkiDirectReadStore
from anki_cli.proto.anki.deck_config import DeckConfigConfig
from anki_cli.proto.anki.decks import (
    DeckCommon,
    DeckFiltered,
    DeckFilteredSearchTerm,
    DeckKindContainer,
    DeckNormal,
)
from anki_cli.proto.anki.notetypes import (
    NotetypeConfig,
    NotetypeConfigCardRequirement,
    NotetypeConfigCardRequirementKind,
    NotetypeConfigKind,
    NotetypeFieldConfig,
    NotetypeTemplateConfig,
)


def _make_store(tmp_path: Path) -> tuple[AnkiDirectReadStore, Path]:
    db_path = tmp_path / "collection.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE col (
            crt INTEGER NOT NULL
        );

        CREATE TABLE deck_config (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            config BLOB NOT NULL
        );

        CREATE TABLE decks (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            common BLOB NOT NULL,
            kind BLOB NOT NULL
        );

        CREATE TABLE cards (
            id INTEGER PRIMARY KEY,
            did INTEGER NOT NULL,
            queue INTEGER NOT NULL,
            due INTEGER NOT NULL
        );

        CREATE TABLE notetypes (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            config BLOB NOT NULL
        );

        CREATE TABLE fields (
            ntid INTEGER NOT NULL,
            ord INTEGER NOT NULL,
            name TEXT NOT NULL,
            config BLOB NOT NULL
        );

        CREATE TABLE templates (
            ntid INTEGER NOT NULL,
            ord INTEGER NOT NULL,
            name TEXT NOT NULL,
            config BLOB NOT NULL
        );

        CREATE TABLE notes (
            id INTEGER PRIMARY KEY,
            mid INTEGER NOT NULL,
            flds TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO col (crt) VALUES (0)")
    conn.commit()
    conn.close()

    return AnkiDirectReadStore(db_path), db_path


def _insert_deck_config(
    db_path: Path,
    *,
    config_id: int,
    name: str,
    new_per_day: int,
    reviews_per_day: int,
    desired_retention: float,
) -> None:
    cfg = DeckConfigConfig(
        new_per_day=new_per_day,
        reviews_per_day=reviews_per_day,
        desired_retention=desired_retention,
    )

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO deck_config (id, name, config) VALUES (?, ?, ?)",
        (config_id, name, bytes(cfg)),
    )
    conn.commit()
    conn.close()


def _insert_deck_normal(
    db_path: Path,
    *,
    did: int,
    name: str,
    config_id: int,
    description: str = "",
    new_limit: int | None = None,
    review_limit: int | None = None,
    stats: tuple[int, int, int] = (0, 0, 0),
) -> None:
    common = DeckCommon(
        new_studied=stats[0],
        review_studied=stats[1],
        learning_studied=stats[2],
    )
    normal = DeckNormal(
        config_id=config_id,
        description=description,
        new_limit=new_limit,
        review_limit=review_limit,
    )
    kind = DeckKindContainer(normal=normal)

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO decks (id, name, common, kind) VALUES (?, ?, ?, ?)",
        (did, name, bytes(common), bytes(kind)),
    )
    conn.commit()
    conn.close()


def _insert_deck_filtered(
    db_path: Path,
    *,
    did: int,
    name: str,
    reschedule: bool,
    searches: list[str],
    stats: tuple[int, int, int] = (0, 0, 0),
) -> None:
    common = DeckCommon(
        new_studied=stats[0],
        review_studied=stats[1],
        learning_studied=stats[2],
    )
    filtered = DeckFiltered(
        reschedule=reschedule,
        search_terms=[
            DeckFilteredSearchTerm(search=s, limit=10) for s in searches
        ],
    )
    kind = DeckKindContainer(filtered=filtered)

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO decks (id, name, common, kind) VALUES (?, ?, ?, ?)",
        (did, name, bytes(common), bytes(kind)),
    )
    conn.commit()
    conn.close()


def _insert_card(db_path: Path, *, cid: int, did: int, queue: int, due: int) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO cards (id, did, queue, due) VALUES (?, ?, ?, ?)",
        (cid, did, queue, due),
    )
    conn.commit()
    conn.close()


def _insert_notetype(
    db_path: Path,
    *,
    ntid: int,
    name: str,
    kind: NotetypeConfigKind,
    sort_field_idx: int,
    css: str,
    reqs: list[NotetypeConfigCardRequirement],
) -> None:
    cfg = NotetypeConfig(
        kind=kind,
        sort_field_idx=sort_field_idx,
        css=css,
        reqs=reqs,
    )

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO notetypes (id, name, config) VALUES (?, ?, ?)",
        (ntid, name, bytes(cfg)),
    )
    conn.commit()
    conn.close()


def _insert_field(db_path: Path, *, ntid: int, ord_: int, name: str) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO fields (ntid, ord, name, config) VALUES (?, ?, ?, ?)",
        (ntid, ord_, name, bytes(NotetypeFieldConfig())),
    )
    conn.commit()
    conn.close()


def _insert_template(
    db_path: Path,
    *,
    ntid: int,
    ord_: int,
    name: str,
    qfmt: str,
    afmt: str,
) -> None:
    cfg = NotetypeTemplateConfig(q_format=qfmt, a_format=afmt)

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO templates (ntid, ord, name, config) VALUES (?, ?, ?, ?)",
        (ntid, ord_, name, bytes(cfg)),
    )
    conn.commit()
    conn.close()


def _insert_note(db_path: Path, *, nid: int, mid: int, flds: str) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO notes (id, mid, flds) VALUES (?, ?, ?)",
        (nid, mid, flds),
    )
    conn.commit()
    conn.close()


def test_get_decks_returns_normal_filtered_and_config_data(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)

    _insert_deck_config(
        db_path,
        config_id=1,
        name="DefaultCfg",
        new_per_day=20,
        reviews_per_day=200,
        desired_retention=0.9,
    )
    _insert_deck_normal(
        db_path,
        did=1,
        name="Default",
        config_id=1,
        description="Main deck",
        new_limit=30,
        review_limit=100,
        stats=(3, 4, 5),
    )
    _insert_deck_filtered(
        db_path,
        did=2,
        name="Filtered",
        reschedule=True,
        searches=["tag:foo", "deck:Default"],
        stats=(0, 1, 2),
    )
    _insert_deck_normal(
        db_path,
        did=3,
        name="OrphanConfig",
        config_id=999,  # no matching deck_config row
    )

    decks = store.get_decks()
    by_name = {str(item["name"]): item for item in decks}

    assert [str(item["name"]) for item in decks] == ["Default", "Filtered", "OrphanConfig"]

    default = by_name["Default"]
    assert default["kind"] == "normal"
    assert default["stats"] == {"new_studied": 3, "review_studied": 4, "learning_studied": 5}
    assert default["config_id"] == 1
    assert default["description"] == "Main deck"
    assert default["new_limit"] == 30
    assert default["review_limit"] == 100
    assert default["config"] == {
        "id": 1,
        "name": "DefaultCfg",
        "new_per_day": 20,
        "reviews_per_day": 200,
        "desired_retention": pytest.approx(0.9, abs=1e-6),
    }

    filtered = by_name["Filtered"]
    assert filtered["kind"] == "filtered"
    assert filtered["search_terms"] == ["tag:foo", "deck:Default"]
    assert filtered["reschedule"] is True

    orphan = by_name["OrphanConfig"]
    assert orphan["kind"] == "normal"
    assert orphan["config_id"] == 999
    assert "config" not in orphan


def test_get_deck_includes_due_counts_and_next_due(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_000_000)

    _insert_deck_config(
        db_path,
        config_id=1,
        name="Cfg",
        new_per_day=20,
        reviews_per_day=200,
        desired_retention=0.9,
    )
    _insert_deck_normal(db_path, did=1, name="Default", config_id=1)

    # In-scope cards for Default (did=1)
    _insert_card(db_path, cid=1, did=1, queue=0, due=999)        # new
    _insert_card(db_path, cid=2, did=1, queue=1, due=100)        # learn due
    _insert_card(db_path, cid=3, did=1, queue=3, due=1_000_100)  # relearn not due
    _insert_card(db_path, cid=4, did=1, queue=2, due=11)         # review due (today=11)
    _insert_card(db_path, cid=5, did=1, queue=2, due=12)         # review not due

    # Out-of-scope deck/card
    _insert_deck_normal(db_path, did=2, name="Other", config_id=1)
    _insert_card(db_path, cid=6, did=2, queue=1, due=0)

    out = store.get_deck("Default")
    assert out["name"] == "Default"
    assert out["due_counts"] == {"new": 1, "learn": 1, "review": 1, "total": 3}
    assert out["next_due"] == {"queue": 1, "epoch_secs": 100}


def test_get_deck_new_only_has_no_next_due(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_000_000)

    _insert_deck_config(
        db_path,
        config_id=1,
        name="Cfg",
        new_per_day=20,
        reviews_per_day=200,
        desired_retention=0.9,
    )
    _insert_deck_normal(db_path, did=1, name="NewOnly", config_id=1)
    _insert_card(db_path, cid=1, did=1, queue=0, due=1)

    out = store.get_deck("NewOnly")
    assert out["due_counts"] == {"new": 1, "learn": 0, "review": 0, "total": 1}
    assert out["next_due"] is None


def test_get_deck_validates_and_missing(tmp_path: Path) -> None:
    store, _db_path = _make_store(tmp_path)

    with pytest.raises(ValueError, match="Deck name cannot be empty"):
        store.get_deck("  ")

    with pytest.raises(LookupError, match="Deck not found"):
        store.get_deck("Missing")


def test_get_notetypes_returns_sorted_counts_and_names(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)

    _insert_notetype(
        db_path,
        ntid=20,
        name="ClozeType",
        kind=NotetypeConfigKind.KIND_CLOZE,
        sort_field_idx=0,
        css="",
        reqs=[],
    )
    _insert_field(db_path, ntid=20, ord_=0, name="Text")
    _insert_field(db_path, ntid=20, ord_=1, name="Extra")
    _insert_template(
        db_path, 
        ntid=20, 
        ord_=0, 
        name="Cloze", 
        qfmt="{{cloze:Text}}", 
        afmt="{{cloze:Text}}<br>{{Extra}}"
    )

    _insert_notetype(
        db_path,
        ntid=10,
        name="Basic",
        kind=NotetypeConfigKind.KIND_NORMAL,
        sort_field_idx=1,
        css=".card {}",
        reqs=[
            NotetypeConfigCardRequirement(
                card_ord=0,
                kind=NotetypeConfigCardRequirementKind.KIND_ALL,
                field_ords=[0, 1],
            )
        ],
    )
    _insert_field(db_path, ntid=10, ord_=0, name="Front")
    _insert_field(db_path, ntid=10, ord_=1, name="Back")
    _insert_template(db_path, ntid=10, ord_=0, name="Card 1", qfmt="{{Front}}", afmt="{{Back}}")

    out = store.get_notetypes()
    assert [item["name"] for item in out] == ["Basic", "ClozeType"]

    basic = out[0]
    assert basic["field_count"] == 2
    assert basic["template_count"] == 1
    assert basic["fields"] == ["Front", "Back"]
    assert basic["templates"] == ["Card 1"]


def test_get_notetype_returns_detailed_payload(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)

    _insert_notetype(
        db_path,
        ntid=10,
        name="Basic",
        kind=NotetypeConfigKind.KIND_NORMAL,
        sort_field_idx=1,
        css=".card { color: red; }",
        reqs=[
            NotetypeConfigCardRequirement(
                card_ord=0,
                kind=NotetypeConfigCardRequirementKind.KIND_ALL,
                field_ords=[0, 1],
            )
        ],
    )
    _insert_field(db_path, ntid=10, ord_=0, name="Front")
    _insert_field(db_path, ntid=10, ord_=1, name="Back")
    _insert_template(db_path, ntid=10, ord_=0, name="Card 1", qfmt="{{Front}}", afmt="{{Back}}")

    out = store.get_notetype("Basic")
    assert out["id"] == 10
    assert out["name"] == "Basic"
    assert out["kind"] == "normal"
    assert out["sort_field_idx"] == 1
    assert out["fields"] == ["Front", "Back"]
    assert out["templates"] == {
        "Card 1": {"Front": "{{Front}}", "Back": "{{Back}}", "ord": 0}
    }
    assert out["styling"] == {"css": ".card { color: red; }"}
    assert out["requirements"] == [
        {
            "card_ord": 0,
            "kind": int(NotetypeConfigCardRequirementKind.KIND_ALL),
            "field_ords": [0, 1]
        }
    ]


def test_get_notetype_missing_raises_lookup_error(tmp_path: Path) -> None:
    store, _db_path = _make_store(tmp_path)

    with pytest.raises(LookupError, match="Notetype not found"):
        store.get_notetype("Missing")


def test_get_note_fields_maps_ordinals_and_pads_missing_values(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)

    _insert_notetype(
        db_path,
        ntid=10,
        name="Basic",
        kind=NotetypeConfigKind.KIND_NORMAL,
        sort_field_idx=0,
        css="",
        reqs=[],
    )
    _insert_field(db_path, ntid=10, ord_=0, name="Front")
    _insert_field(db_path, ntid=10, ord_=1, name="Back")
    _insert_field(db_path, ntid=10, ord_=2, name="Extra")

    _insert_note(db_path, nid=100, mid=10, flds="Question only")

    out = store.get_note_fields(note_id=100)
    assert out == {
        "Front": "Question only",
        "Back": "",
        "Extra": "",
    }


def test_get_note_fields_subset_and_missing_note(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)

    _insert_notetype(
        db_path,
        ntid=10,
        name="Basic",
        kind=NotetypeConfigKind.KIND_NORMAL,
        sort_field_idx=0,
        css="",
        reqs=[],
    )
    _insert_field(db_path, ntid=10, ord_=0, name="Front")
    _insert_field(db_path, ntid=10, ord_=1, name="Back")
    _insert_field(db_path, ntid=10, ord_=2, name="Extra")
    _insert_note(db_path, nid=100, mid=10, flds="Q\x1fA")

    out = store.get_note_fields(note_id=100, fields=[" Extra ", "Front", "", "Missing"])
    assert out == {"Front": "Q", "Extra": ""}

    with pytest.raises(LookupError, match="Note not found"):
        store.get_note_fields(note_id=999)