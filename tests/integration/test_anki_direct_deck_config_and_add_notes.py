from __future__ import annotations

import time
from pathlib import Path
from typing import Any, cast

import pytest

from anki_cli.db.store import AnkiDirectStore
from anki_cli.proto.anki.deck_config import DeckConfigConfig
from anki_cli.proto.anki.decks import DeckFiltered, DeckKindContainer
from tests.anki_schema import connect
from tests.conftest import Collection, new_collection, normal_deck_kind


def _make_store(tmp_path: Path) -> tuple[AnkiDirectStore, Path]:
    col = new_collection(tmp_path / "collection.anki2", seed=False)
    return col.store(writable=False), col.db_path


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
    Collection(db_path).insert_deck_config(
        id=config_id, name=name, mtime_secs=mtime_secs, usn=usn,
        new_per_day=new_per_day, reviews_per_day=reviews_per_day,
        desired_retention=desired_retention, maximum_review_interval=maximum_review_interval,
        learn_steps=learn_steps, relearn_steps=relearn_steps,
    )


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
    Collection(db_path).insert_deck(
        id=did, name=name, mtime_secs=mtime_secs, usn=usn,
        kind=normal_deck_kind(config_id=config_id, description=description),
    )


def _insert_deck_filtered(
    db_path: Path,
    *,
    did: int,
    name: str,
    mtime_secs: int = 1,
    usn: int = 0,
) -> None:
    # Bare filtered deck (no search terms), as the tests always had.
    Collection(db_path).insert_deck(
        id=did, name=name, mtime_secs=mtime_secs, usn=usn,
        kind=bytes(DeckKindContainer(filtered=DeckFiltered())),
    )


def _deck_config_row(db_path: Path, config_id: int) -> dict[str, Any]:
    conn = connect(str(db_path))
    row = conn.execute(
        "SELECT id, name, mtime_secs, usn, config FROM deck_config WHERE id = ?",
        (config_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    return dict(row)


def _deck_row_by_id(db_path: Path, did: int) -> dict[str, Any]:
    conn = connect(str(db_path))
    row = conn.execute(
        "SELECT id, name, mtime_secs, usn FROM decks WHERE id = ?",
        (did,),
    ).fetchone()
    conn.close()
    assert row is not None
    return dict(row)


def _notes_rows(db_path: Path) -> list[dict[str, Any]]:
    conn = connect(str(db_path))
    rows = conn.execute("SELECT id, tags, flds FROM notes ORDER BY id").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def _cards_count(db_path: Path) -> int:
    conn = connect(str(db_path))
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
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000)

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

    row = Collection(db_path).assert_synced("deck_config", 1)
    assert row["mtime_secs"] == 1_700_000_000

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
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000)

    # write_deck/create_deck require at least one template deck row.
    _insert_deck_normal(db_path, did=1, name="Default", config_id=1)

    out = store.create_deck("  NewDeck  ")
    assert out["deck"] == "NewDeck"
    assert out["created"] is True
    did = int(cast(int | str, out["id"]))

    row = Collection(db_path).assert_synced("decks", did)
    assert row["name"] == "NewDeck"
    assert row["mtime_secs"] == 1_700_000_000


def test_add_notes_returns_id_or_none_per_input_item(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000)

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
