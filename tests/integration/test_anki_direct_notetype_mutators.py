from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

import anki_cli.db.anki_direct as direct_mod
from anki_cli.db.anki_direct import AnkiDirectReadStore
from anki_cli.proto.anki.notetypes import (
    NotetypeConfig,
    NotetypeConfigCardRequirementKind,
    NotetypeConfigKind,
    NotetypeTemplateConfig,
)


def _make_store(tmp_path: Path) -> tuple[AnkiDirectReadStore, Path]:
    db_path = tmp_path / "collection.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
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
        """
    )
    conn.commit()
    conn.close()

    return AnkiDirectReadStore(db_path), db_path


def _enable_writes(monkeypatch: pytest.MonkeyPatch, store: AnkiDirectReadStore) -> None:
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)


def _create_basic_notetype(store: AnkiDirectReadStore, *, name: str = "Basic") -> int:
    result = store.create_notetype(
        name=name,
        fields=["Front", "Back"],
        templates=[{"name": "Card 1", "front": "{{Front}}", "back": "{{Back}}"}],
        css="",
        kind="normal",
    )
    return int(cast(int | str, result["id"]))


def _notetype_row_by_id(db_path: Path, ntid: int) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, name, mtime_secs, usn, config FROM notetypes WHERE id = ?",
        (ntid,),
    ).fetchone()
    conn.close()
    assert row is not None
    return {k: row[k] for k in row.keys()}


def _notetype_row_by_name(db_path: Path, name: str) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, name, mtime_secs, usn, config FROM notetypes WHERE name = ?",
        (name,),
    ).fetchone()
    conn.close()
    assert row is not None
    return {k: row[k] for k in row.keys()}


def _fields_for_ntid(db_path: Path, ntid: int) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ord, name, config FROM fields WHERE ntid = ? ORDER BY ord",
        (ntid,),
    ).fetchall()
    conn.close()
    return [{k: row[k] for k in row.keys()} for row in rows]


def _templates_for_ntid(db_path: Path, ntid: int) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ord, name, mtime_secs, usn, config FROM templates WHERE ntid = ? ORDER BY ord",
        (ntid,),
    ).fetchall()
    conn.close()
    return [{k: row[k] for k in row.keys()} for row in rows]


def _notetype_count(db_path: Path, name: str) -> int:
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT COUNT(*) FROM notetypes WHERE name = ?", (name,)).fetchone()
    conn.close()
    assert row is not None
    return int(row[0])


def test_create_notetype_normal_persists_schema_and_requirements(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    _enable_writes(monkeypatch, store)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_700_000_000)

    result = store.create_notetype(
        name="  Basic  ",
        fields=[" Front ", "Back", "   "],
        templates=[{"name": " Card 1 ", "front": "{{Front}}", "back": "{{Back}}"}],
        css=".card { color: red; }",
        kind="normal",
    )

    ntid = int(cast(int | str, result["id"]))
    assert result == {
        "id": ntid,
        "name": "Basic",
        "kind": "normal",
        "field_count": 2,
        "template_count": 1,
        "created": True,
    }

    nt_row = _notetype_row_by_id(db_path, ntid)
    assert nt_row["name"] == "Basic"
    assert nt_row["mtime_secs"] == 1_700_000_000
    assert nt_row["usn"] == -1

    cfg = NotetypeConfig().parse(bytes(nt_row["config"]))
    assert cfg.kind == NotetypeConfigKind.KIND_NORMAL
    assert int(cfg.sort_field_idx) == 0
    assert cfg.css == ".card { color: red; }"
    assert len(cfg.reqs) == 1
    assert int(cfg.reqs[0].card_ord) == 0
    assert cfg.reqs[0].kind == NotetypeConfigCardRequirementKind.KIND_ALL
    assert [int(x) for x in cfg.reqs[0].field_ords] == [0, 1]

    fields = _fields_for_ntid(db_path, ntid)
    assert [(int(row["ord"]), str(row["name"])) for row in fields] == [(0, "Front"), (1, "Back")]

    templates = _templates_for_ntid(db_path, ntid)
    assert len(templates) == 1
    assert int(templates[0]["ord"]) == 0
    assert str(templates[0]["name"]) == "Card 1"
    assert int(templates[0]["mtime_secs"]) == 1_700_000_000
    assert int(templates[0]["usn"]) == -1

    tcfg = NotetypeTemplateConfig().parse(bytes(templates[0]["config"]))
    assert tcfg.q_format == "{{Front}}"
    assert tcfg.a_format == "{{Back}}"


def test_create_notetype_cloze_sets_kind_and_requirement_kind_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    _enable_writes(monkeypatch, store)

    result = store.create_notetype(
        name="ClozeType",
        fields=["Text", "Extra"],
        templates=[
            {"name": "Cloze", "front": "{{cloze:Text}}", "back": "{{cloze:Text}}<br>{{Extra}}"}
        ],
        kind="cloze",
    )

    nt_row = _notetype_row_by_id(db_path, int(cast(int | str, result["id"])))
    cfg = NotetypeConfig().parse(bytes(nt_row["config"]))
    assert cfg.kind == NotetypeConfigKind.KIND_CLOZE
    assert len(cfg.reqs) == 1
    assert cfg.reqs[0].kind == NotetypeConfigCardRequirementKind.KIND_NONE


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {
                "name": " ",
                "fields": ["Front"],
                "templates": [{"name": "Card 1", "front": "{{Front}}", "back": "{{Back}}"}],
            },
            "Notetype name cannot be empty",
        ),
        (
            {
                "name": "X",
                "fields": [" ", ""],
                "templates": [{"name": "Card 1", "front": "{{Front}}", "back": "{{Back}}"}],
            },
            "At least one field is required",
        ),
        (
            {
                "name": "X",
                "fields": ["Front"],
                "templates": [],
            },
            "At least one template is required",
        ),
        (
            {
                "name": "X",
                "fields": ["Front"],
                "templates": [{"name": " ", "front": "Q", "back": "A"}],
            },
            "Template name cannot be empty",
        ),
        (
            {
                "name": "X",
                "fields": ["Front"],
                "templates": [{"name": "Card 1", "front": "Q", "back": "A"}],
                "kind": "weird",
            },
            "kind must be 'normal' or 'cloze'",
        ),
    ],
)
def test_create_notetype_validates_inputs(
    kwargs: dict[str, Any],
    message: str,
    tmp_path: Path,
) -> None:
    store, _db_path = _make_store(tmp_path)

    with pytest.raises(ValueError, match=message):
        store.create_notetype(**kwargs)


def test_create_notetype_duplicate_name_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    _enable_writes(monkeypatch, store)

    _create_basic_notetype(store, name="Basic")

    with pytest.raises(ValueError, match="Notetype already exists"):
        _create_basic_notetype(store, name="Basic")

    assert _notetype_count(db_path, "Basic") == 1


def test_add_notetype_field_adds_next_ord_and_duplicate_noop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    _enable_writes(monkeypatch, store)

    ntid = _create_basic_notetype(store)

    added = store.add_notetype_field(name="Basic", field_name=" Hint ")
    assert added == {"name": "Basic", "field": "Hint", "added": True}

    fields = _fields_for_ntid(db_path, ntid)
    assert [(int(row["ord"]), str(row["name"])) for row in fields] == [
        (0, "Front"),
        (1, "Back"),
        (2, "Hint"),
    ]

    dup = store.add_notetype_field(name="Basic", field_name="Hint")
    assert dup == {"name": "Basic", "field": "Hint", "added": False}

    fields_after = _fields_for_ntid(db_path, ntid)
    assert [(int(row["ord"]), str(row["name"])) for row in fields_after] == [
        (0, "Front"),
        (1, "Back"),
        (2, "Hint"),
    ]


def test_add_notetype_field_missing_notetype_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, _db_path = _make_store(tmp_path)
    _enable_writes(monkeypatch, store)

    with pytest.raises(LookupError, match="Notetype not found"):
        store.add_notetype_field(name="Missing", field_name="X")


def test_remove_notetype_field_removes_and_reorders(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    _enable_writes(monkeypatch, store)

    store.create_notetype(
        name="Tri",
        fields=["A", "B", "C"],
        templates=[{"name": "Card 1", "front": "{{A}}", "back": "{{B}}"}],
    )
    ntid = int(_notetype_row_by_name(db_path, "Tri")["id"])

    result = store.remove_notetype_field(name="Tri", field_name="B")
    assert result == {"name": "Tri", "field": "B", "removed": True}

    fields = _fields_for_ntid(db_path, ntid)
    assert [(int(row["ord"]), str(row["name"])) for row in fields] == [(0, "A"), (1, "C")]


def test_remove_notetype_field_updates_sort_field_idx_when_out_of_range(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    _enable_writes(monkeypatch, store)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_700_000_000)

    store.create_notetype(
        name="Sorty",
        fields=["A", "B", "C"],
        templates=[{"name": "Card 1", "front": "{{A}}", "back": "{{B}}"}],
    )
    nt_row = _notetype_row_by_name(db_path, "Sorty")
    ntid = int(nt_row["id"])

    cfg = NotetypeConfig().parse(bytes(nt_row["config"]))
    cfg.sort_field_idx = 2
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE notetypes SET config = ? WHERE id = ?", (bytes(cfg), ntid))
    conn.commit()
    conn.close()

    store.remove_notetype_field(name="Sorty", field_name="C")

    updated_cfg = NotetypeConfig().parse(bytes(_notetype_row_by_id(db_path, ntid)["config"]))
    assert int(updated_cfg.sort_field_idx) == 1


def test_remove_notetype_field_validates_last_remaining_and_missing_field(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, _db_path = _make_store(tmp_path)
    _enable_writes(monkeypatch, store)

    store.create_notetype(
        name="Single",
        fields=["Only"],
        templates=[{"name": "Card 1", "front": "{{Only}}", "back": "{{Only}}"}],
    )
    with pytest.raises(ValueError, match="Cannot remove the last remaining field"):
        store.remove_notetype_field(name="Single", field_name="Only")

    _create_basic_notetype(store, name="Basic2")
    with pytest.raises(LookupError, match="Field not found"):
        store.remove_notetype_field(name="Basic2", field_name="Nope")


def test_add_notetype_template_adds_next_ord_and_duplicate_noop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    _enable_writes(monkeypatch, store)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_700_000_000)

    ntid = _create_basic_notetype(store)

    added = store.add_notetype_template(
        name="Basic",
        template_name=" Card 2 ",
        front="{{Back}}",
        back="{{Front}}",
    )
    assert added == {"name": "Basic", "template": "Card 2", "added": True}

    templates = _templates_for_ntid(db_path, ntid)
    assert [
        (int(row["ord"]), str(row["name"])) for row in templates
    ] == [(0, "Card 1"), (1, "Card 2")]

    tcfg = NotetypeTemplateConfig().parse(bytes(templates[1]["config"]))
    assert tcfg.q_format == "{{Back}}"
    assert tcfg.a_format == "{{Front}}"
    assert int(templates[1]["mtime_secs"]) == 1_700_000_000
    assert int(templates[1]["usn"]) == -1

    dup = store.add_notetype_template(
        name="Basic",
        template_name="Card 2",
        front="x",
        back="y",
    )
    assert dup == {"name": "Basic", "template": "Card 2", "added": False}


def test_add_notetype_template_missing_notetype_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, _db_path = _make_store(tmp_path)
    _enable_writes(monkeypatch, store)

    with pytest.raises(LookupError, match="Notetype not found"):
        store.add_notetype_template(name="Missing", template_name="Card 1", front="Q", back="A")


def test_edit_notetype_template_updates_front_and_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    _enable_writes(monkeypatch, store)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_700_000_000)

    ntid = _create_basic_notetype(store)

    out1 = store.edit_notetype_template(
        name="Basic",
        template_name="Card 1",
        front="Q2",
    )
    assert out1 == {"name": "Basic", "template": "Card 1", "updated": True}

    cfg1 = NotetypeTemplateConfig().parse(bytes(_templates_for_ntid(db_path, ntid)[0]["config"]))
    assert cfg1.q_format == "Q2"
    assert cfg1.a_format == "{{Back}}"

    out2 = store.edit_notetype_template(
        name="Basic",
        template_name="Card 1",
        back="A2",
    )
    assert out2 == {"name": "Basic", "template": "Card 1", "updated": True}

    cfg2 = NotetypeTemplateConfig().parse(bytes(_templates_for_ntid(db_path, ntid)[0]["config"]))
    assert cfg2.q_format == "Q2"
    assert cfg2.a_format == "A2"


def test_edit_notetype_template_validates_inputs_and_missing_template(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, _db_path = _make_store(tmp_path)
    _enable_writes(monkeypatch, store)

    _create_basic_notetype(store)

    with pytest.raises(ValueError, match="Provide at least one of front/back"):
        store.edit_notetype_template(name="Basic", template_name="Card 1")

    with pytest.raises(LookupError, match="Template not found"):
        store.edit_notetype_template(name="Basic", template_name="Missing", front="Q")


def test_set_notetype_css_updates_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    _enable_writes(monkeypatch, store)
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_700_000_000)

    ntid = _create_basic_notetype(store)

    result = store.set_notetype_css(name="Basic", css=".card{font-size:20px}")
    assert result == {"name": "Basic", "updated": True, "css": ".card{font-size:20px}"}

    nt_cfg = NotetypeConfig().parse(bytes(_notetype_row_by_id(db_path, ntid)["config"]))
    assert nt_cfg.css == ".card{font-size:20px}"
    assert int(_notetype_row_by_id(db_path, ntid)["mtime_secs"]) == 1_700_000_000
    assert int(_notetype_row_by_id(db_path, ntid)["usn"]) == -1


def test_set_notetype_css_validates_and_missing_notetype(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, _db_path = _make_store(tmp_path)
    _enable_writes(monkeypatch, store)

    with pytest.raises(ValueError, match="Notetype name cannot be empty"):
        store.set_notetype_css(name=" ", css="x")

    with pytest.raises(LookupError, match="Notetype not found"):
        store.set_notetype_css(name="Missing", css="x")