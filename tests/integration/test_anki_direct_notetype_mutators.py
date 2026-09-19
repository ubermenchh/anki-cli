from __future__ import annotations

import time
from pathlib import Path
from typing import Any, cast

import pytest

from anki_cli.db.store import AnkiDirectStore
from anki_cli.proto.anki.notetypes import (
    NotetypeConfig,
    NotetypeConfigCardRequirement,
    NotetypeConfigCardRequirementKind,
    NotetypeConfigKind,
    NotetypeTemplateConfig,
)
from tests.anki_schema import connect
from tests.conftest import Collection, new_collection
from tests.integration.conftest import CSUM_A, CSUM_B, assert_col_modified, col_row


def _make_store(tmp_path: Path) -> tuple[AnkiDirectStore, Path]:
    """Bare schema; every notetype here is created through the store."""
    col = new_collection(tmp_path / "collection.anki2", seed=False)
    return col.store(writable=False), col.db_path


def _insert_note(db_path: Path, *, note_id: int, mid: int, fields: list[str]) -> None:
    # mod/usn at the DDL defaults this file always used (0/0), so a mutator's
    # re-flagging is observable.
    Collection(db_path).insert_note(id=note_id, mid=mid, fields=fields, mod=0, usn=0)


def _note_row(db_path: Path, note_id: int) -> dict[str, Any]:
    conn = connect(str(db_path))
    row = conn.execute(
        "SELECT id, mid, mod, usn, flds, sfld, csum FROM notes WHERE id = ?",
        (note_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    return dict(row)


def _enable_writes(monkeypatch: pytest.MonkeyPatch, store: AnkiDirectStore) -> None:
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)


def _create_basic_notetype(store: AnkiDirectStore, *, name: str = "Basic") -> int:
    result = store.create_notetype(
        name=name,
        fields=["Front", "Back"],
        templates=[{"name": "Card 1", "front": "{{Front}}", "back": "{{Back}}"}],
        css="",
        kind="normal",
    )
    ntid = int(cast(int | str, result["id"]))
    _mark_synced(store.db_path, ntid)
    return ntid


def _mark_synced(db_path: Path, ntid: int) -> None:
    """Pretend a sync completed: mutators must re-flag usn = -1 themselves.

    Without this, the ``usn == -1`` written by ``create_notetype`` would satisfy
    every later assertion before the mutator under test even runs.
    """
    conn = connect(str(db_path))
    conn.execute("UPDATE notetypes SET usn = 0, mtime_secs = 0 WHERE id = ?", (ntid,))
    conn.execute("UPDATE templates SET usn = 0, mtime_secs = 0 WHERE ntid = ?", (ntid,))
    conn.commit()
    conn.close()


def _notetype_row_by_id(db_path: Path, ntid: int) -> dict[str, Any]:
    conn = connect(str(db_path))
    row = conn.execute(
        "SELECT id, name, mtime_secs, usn, config FROM notetypes WHERE id = ?",
        (ntid,),
    ).fetchone()
    conn.close()
    assert row is not None
    return dict(row)


def _notetype_row_by_name(db_path: Path, name: str) -> dict[str, Any]:
    conn = connect(str(db_path))
    row = conn.execute(
        "SELECT id, name, mtime_secs, usn, config FROM notetypes WHERE name = ?",
        (name,),
    ).fetchone()
    conn.close()
    assert row is not None
    return dict(row)


def _fields_for_ntid(db_path: Path, ntid: int) -> list[dict[str, Any]]:
    conn = connect(str(db_path))
    rows = conn.execute(
        "SELECT ord, name, config FROM fields WHERE ntid = ? ORDER BY ord",
        (ntid,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def _templates_for_ntid(db_path: Path, ntid: int) -> list[dict[str, Any]]:
    conn = connect(str(db_path))
    rows = conn.execute(
        "SELECT ord, name, mtime_secs, usn, config FROM templates WHERE ntid = ? ORDER BY ord",
        (ntid,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def _notetype_count(db_path: Path, name: str) -> int:
    conn = connect(str(db_path))
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
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000)

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
    # Requirements are derived from the template like Anki does: a front of
    # {{Front}} renders when Front alone is non-empty -> ANY [0].
    assert len(cfg.reqs) == 1
    assert int(cfg.reqs[0].card_ord) == 0
    assert cfg.reqs[0].kind == NotetypeConfigCardRequirementKind.KIND_ANY
    assert [int(x) for x in cfg.reqs[0].field_ords] == [0]

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


def test_create_notetype_cloze_sets_kind_and_any_requirement(
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
    # Stock Anki's Cloze notetype stores req [[0, "any", [0]]]: {{cloze:Text}}
    # strips to key "Text" (ord 0).
    assert len(cfg.reqs) == 1
    assert cfg.reqs[0].kind == NotetypeConfigCardRequirementKind.KIND_ANY
    assert [int(x) for x in cfg.reqs[0].field_ords] == [0]


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

    _insert_note(db_path, note_id=100, mid=ntid, fields=["q", "a"])
    # A short (legacy) row must be padded to the old count before the new slot.
    _insert_note(db_path, note_id=101, mid=ntid, fields=["only"])
    # An over-long (malformed) row is truncated first, as Anki's reorder_fields does.
    _insert_note(db_path, note_id=102, mid=ntid, fields=["x", "y", "stale"])

    added = store.add_notetype_field(name="Basic", field_name=" Hint ")
    assert added == {
        "name": "Basic",
        "field": "Hint",
        "added": True,
        "updated_notes": 3,
        "full_sync_required": True,
    }
    # Regression for #42: every note gains an empty trailing slot.
    assert _note_row(db_path, 100)["flds"] == "q\x1fa\x1f"
    assert _note_row(db_path, 100)["usn"] == -1
    assert _note_row(db_path, 101)["flds"] == "only\x1f\x1f"
    assert _note_row(db_path, 102)["flds"] == "x\x1fy\x1f"
    assert store.get_note_fields(note_id=100) == {"Front": "q", "Back": "a", "Hint": ""}
    assert _notetype_row_by_id(db_path, ntid)["usn"] == -1
    assert_col_modified(db_path, schema=True)

    fields = _fields_for_ntid(db_path, ntid)
    assert [(int(row["ord"]), str(row["name"])) for row in fields] == [
        (0, "Front"),
        (1, "Back"),
        (2, "Hint"),
    ]

    before = col_row(db_path)
    dup = store.add_notetype_field(name="Basic", field_name="Hint")
    assert dup == {"name": "Basic", "field": "Hint", "added": False}
    # A no-op must not move col.mod, and above all must not force a full sync.
    assert col_row(db_path) == before

    fields_after = _fields_for_ntid(db_path, ntid)
    assert [(int(row["ord"]), str(row["name"])) for row in fields_after] == [
        (0, "Front"),
        (1, "Back"),
        (2, "Hint"),
    ]


def test_add_notetype_field_pads_to_field_count_not_max_ord(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A malformed collection with a gap in fields.ord must not over-pad notes."""
    store, db_path = _make_store(tmp_path)
    _enable_writes(monkeypatch, store)
    ntid = _create_basic_notetype(store)
    conn = connect(str(db_path))
    conn.execute("UPDATE fields SET ord = 2 WHERE ntid = ? AND ord = 1", (ntid,))  # ords 0, 2
    conn.commit()
    conn.close()
    _insert_note(db_path, note_id=100, mid=ntid, fields=["q", "a"])

    store.add_notetype_field(name="Basic", field_name="Hint")

    # Two fields existed, so the note must end up with exactly three slots.
    assert _note_row(db_path, 100)["flds"] == "q\x1fa\x1f"


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
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000)

    store.create_notetype(
        name="Tri",
        fields=["A", "B", "C"],
        templates=[{"name": "Card 1", "front": "{{A}}", "back": "{{B}}"}],
    )
    ntid = int(_notetype_row_by_name(db_path, "Tri")["id"])

    result = store.remove_notetype_field(name="Tri", field_name="B")
    assert result == {
        "name": "Tri",
        "field": "B",
        "removed": True,
        "updated_notes": 0,
        "full_sync_required": True,
    }
    assert_col_modified(db_path, schema=True)
    # scm is a millisecond epoch like mod.
    assert col_row(db_path)["scm"] == 1_700_000_000_000

    fields = _fields_for_ntid(db_path, ntid)
    assert [(int(row["ord"]), str(row["name"])) for row in fields] == [(0, "A"), (1, "C")]


def test_remove_notetype_field_rewrites_note_field_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression for #17: notes.flds is positional and must drop the removed slot."""
    store, db_path = _make_store(tmp_path)
    _enable_writes(monkeypatch, store)
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000)

    store.create_notetype(
        name="Tri",
        fields=["Front", "Extra", "Back"],
        templates=[{"name": "Card 1", "front": "{{Front}}", "back": "{{Back}}"}],
    )
    ntid = int(_notetype_row_by_name(db_path, "Tri")["id"])
    _insert_note(db_path, note_id=100, mid=ntid, fields=["<b>a</b>", "b", "c"])
    _insert_note(db_path, note_id=101, mid=ntid, fields=["x", "y"])  # short row gets padded
    _insert_note(db_path, note_id=200, mid=ntid + 1, fields=["other", "type"])  # untouched

    result = store.remove_notetype_field(name="Tri", field_name="Extra")
    assert result["updated_notes"] == 2

    note = _note_row(db_path, 100)
    assert note["flds"] == "<b>a</b>\x1fc"
    assert note["sfld"] == "a"  # stripped, like the checksum
    assert note["csum"] == CSUM_A
    assert note["mod"] == 1_700_000_000
    assert note["usn"] == -1

    short = _note_row(db_path, 101)
    assert short["flds"] == "x\x1f"

    other = _note_row(db_path, 200)
    assert other["flds"] == "other\x1ftype"
    assert other["usn"] == 0

    # The notetype itself is marked modified so sync picks up the schema change.
    nt_row = _notetype_row_by_id(db_path, ntid)
    assert nt_row["usn"] == -1
    assert nt_row["mtime_secs"] == 1_700_000_000

    # get_note_fields must map the remaining names onto the right values.
    assert store.get_note_fields(note_id=100) == {"Front": "<b>a</b>", "Back": "c"}


def test_remove_notetype_field_matches_name_case_insensitively(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """fields.name is COLLATE unicase in Anki; add_notetype_field already honors that."""
    store, db_path = _make_store(tmp_path)
    _enable_writes(monkeypatch, store)
    ntid = _create_basic_notetype(store)
    _insert_note(db_path, note_id=100, mid=ntid, fields=["q", "a"])

    result = store.remove_notetype_field(name="Basic", field_name="back")
    # The canonical stored name is echoed back, not the caller's spelling.
    assert result["field"] == "Back"
    assert result["updated_notes"] == 1
    assert [str(row["name"]) for row in _fields_for_ntid(db_path, ntid)] == ["Front"]
    assert _note_row(db_path, 100)["flds"] == "q"


def test_remove_notetype_field_removing_the_sort_field_keeps_its_ordinal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Anki's reposition_sort_idx keeps the ordinal when the sort field itself is
    removed (and it wasn't last), so the field sliding into that slot takes over."""
    store, db_path = _make_store(tmp_path)
    _enable_writes(monkeypatch, store)

    store.create_notetype(
        name="Tri",
        fields=["A", "B", "C"],
        templates=[{"name": "Card 1", "front": "{{A}}", "back": "{{B}}"}],
    )
    nt_row = _notetype_row_by_name(db_path, "Tri")
    ntid = int(nt_row["id"])
    cfg = NotetypeConfig().parse(bytes(nt_row["config"]))
    cfg.sort_field_idx = 1  # "B"
    conn = connect(str(db_path))
    conn.execute("UPDATE notetypes SET config = ? WHERE id = ?", (bytes(cfg), ntid))
    conn.commit()
    conn.close()
    _insert_note(db_path, note_id=100, mid=ntid, fields=["a", "b", "c"])

    store.remove_notetype_field(name="Tri", field_name="B")

    after = NotetypeConfig().parse(bytes(_notetype_row_by_id(db_path, ntid)["config"]))
    assert int(after.sort_field_idx) == 1  # now "C", not "A"
    note = _note_row(db_path, 100)
    assert note["flds"] == "a\x1fc"
    assert note["sfld"] == "c"


def test_remove_notetype_field_removing_first_field_recomputes_sfld_and_csum(
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
    _insert_note(db_path, note_id=100, mid=ntid, fields=["a", "b", "c"])

    store.remove_notetype_field(name="Tri", field_name="A")

    note = _note_row(db_path, 100)
    assert note["flds"] == "b\x1fc"
    assert note["sfld"] == "b"
    assert note["csum"] == CSUM_B


def test_remove_notetype_field_shifts_sort_idx_and_requirement_ords(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    _enable_writes(monkeypatch, store)

    store.create_notetype(
        name="Quad",
        fields=["A", "B", "C", "D"],
        templates=[
            {"name": "Card 1", "front": "{{A}}", "back": "{{B}}"},
            {"name": "Card 2", "front": "{{B}}", "back": "{{C}}"},
        ],
    )
    nt_row = _notetype_row_by_name(db_path, "Quad")
    ntid = int(nt_row["id"])

    cfg = NotetypeConfig().parse(bytes(nt_row["config"]))
    cfg.sort_field_idx = 2  # "C"
    # Requirements as Anki would compute them from the templates above.
    cfg.reqs = [
        NotetypeConfigCardRequirement(
            card_ord=0, kind=NotetypeConfigCardRequirementKind.KIND_ANY, field_ords=[0]
        ),
        NotetypeConfigCardRequirement(
            card_ord=1, kind=NotetypeConfigCardRequirementKind.KIND_ANY, field_ords=[1]
        ),
        # Ords past the removed one must shift down by one.
        NotetypeConfigCardRequirement(
            card_ord=2, kind=NotetypeConfigCardRequirementKind.KIND_ALL, field_ords=[0, 2, 3]
        ),
    ]
    conn = connect(str(db_path))
    conn.execute("UPDATE notetypes SET config = ? WHERE id = ?", (bytes(cfg), ntid))
    conn.commit()
    conn.close()

    _insert_note(db_path, note_id=100, mid=ntid, fields=["a", "b", "c", "d"])

    store.remove_notetype_field(name="Quad", field_name="B")

    after = NotetypeConfig().parse(bytes(_notetype_row_by_id(db_path, ntid)["config"]))
    # Sort field "C" moved from ord 2 to ord 1.
    assert int(after.sort_field_idx) == 1
    # Anki rewrites the templates first: Card 2's front {{B}} loses its only
    # field and gets the first remaining field appended ({{A}}); its back
    # {{C}} is untouched. reqs are then recomputed from the result.
    templates = _templates_for_ntid(db_path, ntid)
    card2 = NotetypeTemplateConfig().parse(bytes(templates[1]["config"]))
    assert card2.q_format == "{{A}}"
    assert card2.a_format == "{{C}}"
    assert int(templates[1]["usn"]) == -1
    assert [list(req.field_ords) for req in after.reqs] == [[0], [0]]
    assert all(req.kind == NotetypeConfigCardRequirementKind.KIND_ANY for req in after.reqs)

    note = _note_row(db_path, 100)
    assert note["flds"] == "a\x1fc\x1fd"
    # sfld follows the sort field; csum always hashes the first field.
    assert note["sfld"] == "c"
    assert note["csum"] == CSUM_A


def test_remove_notetype_field_updates_sort_field_idx_when_out_of_range(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    _enable_writes(monkeypatch, store)
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000)

    store.create_notetype(
        name="Sorty",
        fields=["A", "B", "C"],
        templates=[{"name": "Card 1", "front": "{{A}}", "back": "{{B}}"}],
    )
    nt_row = _notetype_row_by_name(db_path, "Sorty")
    ntid = int(nt_row["id"])

    cfg = NotetypeConfig().parse(bytes(nt_row["config"]))
    cfg.sort_field_idx = 2
    conn = connect(str(db_path))
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
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000)

    ntid = _create_basic_notetype(store)

    added = store.add_notetype_template(
        name="Basic",
        template_name=" Card 2 ",
        front="{{Back}}",
        back="{{Front}}",
    )
    assert added == {
        "name": "Basic",
        "template": "Card 2",
        "added": True,
        "full_sync_required": True,
    }
    assert _notetype_row_by_id(db_path, ntid)["usn"] == -1
    assert_col_modified(db_path, schema=True)

    templates = _templates_for_ntid(db_path, ntid)
    assert [
        (int(row["ord"]), str(row["name"])) for row in templates
    ] == [(0, "Card 1"), (1, "Card 2")]

    tcfg = NotetypeTemplateConfig().parse(bytes(templates[1]["config"]))
    assert tcfg.q_format == "{{Back}}"
    assert tcfg.a_format == "{{Front}}"
    assert int(templates[1]["mtime_secs"]) == 1_700_000_000
    assert int(templates[1]["usn"]) == -1

    before = col_row(db_path)
    dup = store.add_notetype_template(
        name="Basic",
        template_name="Card 2",
        front="x",
        back="y",
    )
    assert dup == {"name": "Basic", "template": "Card 2", "added": False}
    assert col_row(db_path) == before


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
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000)

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

    # Editing template text is not a schema change (no forced full sync), but
    # the notetype row must be flagged so a normal sync ships it.
    nt_row = _notetype_row_by_id(db_path, ntid)
    assert nt_row["usn"] == -1
    assert nt_row["mtime_secs"] > 0
    assert_col_modified(db_path, schema=False)


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
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000)

    ntid = _create_basic_notetype(store)

    result = store.set_notetype_css(name="Basic", css=".card{font-size:20px}")
    assert result == {"name": "Basic", "updated": True, "css": ".card{font-size:20px}"}

    nt_cfg = NotetypeConfig().parse(bytes(_notetype_row_by_id(db_path, ntid)["config"]))
    assert nt_cfg.css == ".card{font-size:20px}"
    assert int(_notetype_row_by_id(db_path, ntid)["mtime_secs"]) == 1_700_000_000
    assert int(_notetype_row_by_id(db_path, ntid)["usn"]) == -1
    assert_col_modified(db_path, schema=False)


def test_create_notetype_is_not_a_schema_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Anki's add_notetype does not call set_schema_modified."""
    store, db_path = _make_store(tmp_path)
    _enable_writes(monkeypatch, store)

    _create_basic_notetype(store)

    assert_col_modified(db_path, schema=False)


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
