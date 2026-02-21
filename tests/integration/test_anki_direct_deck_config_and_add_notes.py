from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

import anki_cli.db.anki_direct as direct_mod
from anki_cli.db.anki_direct import AnkiDirectReadStore
from anki_cli.proto.anki.deck_config import DeckConfigConfig
from anki_cli.proto.anki.decks import DeckCommon, DeckFiltered, DeckKindContainer, DeckNormal


def _make_store(tmp_path: Path) -> tuple[AnkiDirectReadStore, Path]:
    db_path = tmp_path / "collection.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE deck_config (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            mtime_secs INTEGER NOT NULL,
            usn INTEGER NOT NULL,
            config BLOB NOT NULL
        );

        CREATE TABLE decks (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            mtime_secs INTEGER NOT NULL,
            usn INTEGER NOT NULL,
            common BLOB NOT NULL,
            kind BLOB NOT NULL
        );

        CREATE TABLE notetypes (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            mtime_secs INTEGER NOT NULL,
            usn INTEGER NOT NULL,
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
            mtime_secs INTEGER NOT NULL,
            usn INTEGER NOT NULL,
            config BLOB NOT NULL
        );

        CREATE TABLE notes (
            id INTEGER PRIMARY KEY,
            guid TEXT NOT NULL,
            mid INTEGER NOT NULL,
            mod INTEGER NOT NULL,
            usn INTEGER NOT NULL,
            tags TEXT NOT NULL,
            flds TEXT NOT NULL,
            sfld TEXT NOT NULL,
            csum INTEGER NOT NULL,
            flags INTEGER NOT NULL,
            data TEXT NOT NULL
        );

        CREATE TABLE cards (
            id INTEGER PRIMARY KEY,
            nid INTEGER NOT NULL,
            did INTEGER NOT NULL,
            ord INTEGER NOT NULL,
            mod INTEGER NOT NULL,
            usn INTEGER NOT NULL,
            type INTEGER NOT NULL,
            queue INTEGER NOT NULL,
            due INTEGER NOT NULL,
            ivl INTEGER NOT NULL,
            factor INTEGER NOT NULL,
            reps INTEGER NOT NULL,
            lapses INTEGER NOT NULL,
            left INTEGER NOT NULL,
            odue INTEGER NOT NULL,
            odid INTEGER NOT NULL,
            flags INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    return AnkiDirectReadStore(db_path), db_path


def _insert_deck_config(
    db_path: Path,
    *,
    config_id: int,
    name: str,
    mtime_secs: int = 1,
    usn: int = 0,
    new_per_day: int = 20,
    reviews_per_day: int = 200,
    desired_retention: float = 0.9,
    maximum_review_interval: int = 36500,
    learn_steps: list[float] | None = None,
    relearn_steps: list[float] | None = None,
) -> None:
    cfg = DeckConfigConfig(
        new_per_day=new_per_day,
        reviews_per_day=reviews_per_day,
        desired_retention=desired_retention,
        maximum_review_interval=maximum_review_interval,
        learn_steps=learn_steps or [1.0, 10.0],
        relearn_steps=relearn_steps or [10.0],
    )
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO deck_config (id, name, mtime_secs, usn, config)
        VALUES (?, ?, ?, ?, ?)
        """,
        (config_id, name, mtime_secs, usn, bytes(cfg)),
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
    mtime_secs: int = 1,
    usn: int = 0,
) -> None:
    common = DeckCommon()
    kind = DeckKindContainer(normal=DeckNormal(config_id=config_id, description=description))
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO decks (id, name, mtime_secs, usn, common, kind)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (did, name, mtime_secs, usn, bytes(common), bytes(kind)),
    )
    conn.commit()
    conn.close()


def _insert_deck_filtered(
    db_path: Path,
    *,
    did: int,
    name: str,
    mtime_secs: int = 1,
    usn: int = 0,
) -> None:
    common = DeckCommon()
    kind = DeckKindContainer(filtered=DeckFiltered())
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO decks (id, name, mtime_secs, usn, common, kind)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (did, name, mtime_secs, usn, bytes(common), bytes(kind)),
    )
    conn.commit()
    conn.close()


def _deck_config_row(db_path: Path, config_id: int) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, name, mtime_secs, usn, config FROM deck_config WHERE id = ?",
        (config_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    return {k: row[k] for k in row.keys()}


def _deck_row_by_id(db_path: Path, did: int) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, name, mtime_secs, usn FROM decks WHERE id = ?",
        (did,),
    ).fetchone()
    conn.close()
    assert row is not None
    return {k: row[k] for k in row.keys()}


def _notes_rows(db_path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, tags, flds FROM notes ORDER BY id").fetchall()
    conn.close()
    return [{k: row[k] for k in row.keys()} for row in rows]


def _cards_count(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT COUNT(*) FROM cards").fetchone()
    conn.close()
    assert row is not None
    return int(row[0])


def test_get_deck_config_success(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)

    _insert_deck_config(
        db_path,
        config_id=1,
        name="DefaultCfg",
        new_per_day=30,
        reviews_per_day=400,
        desired_retention=0.92,
        maximum_review_interval=999,
        learn_steps=[1.0, 15.0],
        relearn_steps=[10.0, 20.0],
    )
    _insert_deck_normal(db_path, did=1, name="Default", config_id=1)

    out = store.get_deck_config("  Default  ")
    assert out["deck"] == "Default"
    assert out["config_id"] == 1
    assert out["config_name"] == "DefaultCfg"
    config = cast(dict[str, Any], out["config"])
    assert config["new_per_day"] == 30
    assert config["reviews_per_day"] == 400
    assert config["desired_retention"] == pytest.approx(0.92, abs=1e-6)
    assert config["maximum_review_interval"] == 999
    assert config["learn_steps"] == [1.0, 15.0]
    assert config["relearn_steps"] == [10.0, 20.0]


def test_get_deck_config_validation_and_missing_cases(tmp_path: Path) -> None:
    store, db_path = _make_store(tmp_path)

    with pytest.raises(ValueError, match="Deck name cannot be empty"):
        store.get_deck_config(" ")

    with pytest.raises(LookupError, match="Deck not found"):
        store.get_deck_config("Missing")

    _insert_deck_filtered(db_path, did=2, name="Filtered")
    with pytest.raises(ValueError, match="is not a normal deck"):
        store.get_deck_config("Filtered")

    _insert_deck_normal(db_path, did=3, name="NoConfig", config_id=999)
    with pytest.raises(LookupError, match="Deck config not found: 999"):
        store.get_deck_config("NoConfig")


def test_set_deck_config_no_updates_returns_noop(tmp_path: Path) -> None:
    store, _db_path = _make_store(tmp_path)

    assert store.set_deck_config(name="Default", updates={}) == {
        "deck": "Default",
        "updated": False,
        "config": {},
    }


def test_set_deck_config_updates_fields_and_persists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_700_000_000)

    _insert_deck_config(
        db_path,
        config_id=1,
        name="DefaultCfg",
        new_per_day=20,
        reviews_per_day=200,
        desired_retention=0.9,
        maximum_review_interval=36500,
        learn_steps=[1.0, 10.0],
        relearn_steps=[10.0],
    )
    _insert_deck_normal(db_path, did=1, name="Default", config_id=1)

    out = store.set_deck_config(
        name="Default",
        updates={
            "new_per_day": "30",
            "reviews_per_day": 500.0,
            "desired_retention": "0.95",
            "maximum_review_interval": 800,
            "learn_steps": "1, 5, 15",
            "relearn_steps": [10, 30.5],
        },
    )

    assert out["deck"] == "Default"
    assert out["updated"] is True
    assert out["config_id"] == 1
    assert out["applied"] == {
        "new_per_day": 30,
        "reviews_per_day": 500,
        "desired_retention": 0.95,
        "maximum_review_interval": 800,
        "learn_steps": [1.0, 5.0, 15.0],
        "relearn_steps": [10.0, 30.5],
    }
    config = cast(dict[str, Any], out["config"])
    assert config["new_per_day"] == 30
    assert config["reviews_per_day"] == 500
    assert config["desired_retention"] == pytest.approx(0.95, abs=1e-6)
    assert config["maximum_review_interval"] == 800
    assert config["learn_steps"] == [1.0, 5.0, 15.0]
    assert config["relearn_steps"] == [10.0, 30.5]

    row = _deck_config_row(db_path, 1)
    assert row["mtime_secs"] == 1_700_000_000
    assert row["usn"] == -1

    cfg = DeckConfigConfig().parse(bytes(row["config"]))
    assert int(cfg.new_per_day) == 30
    assert int(cfg.reviews_per_day) == 500
    assert float(cfg.desired_retention) == pytest.approx(0.95, abs=1e-6)
    assert int(cfg.maximum_review_interval) == 800
    assert [float(x) for x in cfg.learn_steps] == [1.0, 5.0, 15.0]
    assert [float(x) for x in cfg.relearn_steps] == [10.0, 30.5]


def test_set_deck_config_validation_and_missing_cases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    with pytest.raises(ValueError, match="Deck name cannot be empty"):
        store.set_deck_config(name=" ", updates={"new_per_day": 1})

    with pytest.raises(LookupError, match="Deck not found"):
        store.set_deck_config(name="Missing", updates={"new_per_day": 1})

    _insert_deck_filtered(db_path, did=2, name="Filtered")
    with pytest.raises(ValueError, match="is not a normal deck"):
        store.set_deck_config(name="Filtered", updates={"new_per_day": 1})

    _insert_deck_normal(db_path, did=3, name="NoConfig", config_id=999)
    with pytest.raises(LookupError, match="Deck config not found: 999"):
        store.set_deck_config(name="NoConfig", updates={"new_per_day": 1})

    _insert_deck_config(db_path, config_id=1, name="Cfg")
    _insert_deck_normal(db_path, did=1, name="Default", config_id=1)

    with pytest.raises(ValueError, match="Unsupported deck config key"):
        store.set_deck_config(name="Default", updates={"unknown": 1})

    with pytest.raises(ValueError, match="new_per_day must be an integer"):
        store.set_deck_config(name="Default", updates={"new_per_day": "abc"})

    with pytest.raises(ValueError, match="desired_retention must be a float"):
        store.set_deck_config(name="Default", updates={"desired_retention": "abc"})

    with pytest.raises(ValueError, match="Step values must be numeric"):
        store.set_deck_config(name="Default", updates={"learn_steps": ["ok", "nope"]})


def test_create_deck_alias_creates_deck(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_700_000_000)

    # write_deck/create_deck require at least one template deck row.
    _insert_deck_normal(db_path, did=1, name="Default", config_id=1)

    out = store.create_deck("  NewDeck  ")
    assert out["deck"] == "NewDeck"
    assert out["created"] is True
    did = int(cast(int | str, out["id"]))

    row = _deck_row_by_id(db_path, did)
    assert row["name"] == "NewDeck"
    assert row["mtime_secs"] == 1_700_000_000
    assert row["usn"] == -1


def test_add_notes_returns_id_or_none_per_input_item(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_700_000_000)

    _insert_deck_normal(db_path, did=1, name="Default", config_id=1)

    store.create_notetype(
        name="Basic",
        fields=["Front", "Back"],
        templates=[{"name": "Card 1", "front": "{{Front}}", "back": "{{Back}}"}],
    )

    out = store.add_notes(
        [
            {
                "deck": "Default",
                "notetype": "Basic",
                "fields": {"Front": "Q1", "Back": "A1"},
                "tags": "tag1, tag2",
            },
            {
                "deck": "Default",
                "notetype": "Basic",
                "fields": {"Front": "Q2"},  # missing Back -> add_note raises
            },
            {
                "deck": "MissingDeck",
                "notetype": "Basic",
                "fields": {"Front": "Q3", "Back": "A3"},  # add_note raises
            },
            {
                "deck": "Default",
                "notetype": "Basic",
                "fields": "not-a-dict",  # pre-validation failure
            },
            {
                "notetype": "Basic",
                "fields": {"Front": "Q4", "Back": "A4"},  # missing deck
            },
        ]
    )

    assert isinstance(out[0], int)
    assert out[1:] == [None, None, None, None]

    notes = _notes_rows(db_path)
    assert len(notes) == 1
    assert notes[0]["flds"] == "Q1\x1fA1"
    assert notes[0]["tags"] == " tag1 tag2 "
    assert _cards_count(db_path) == 1