from __future__ import annotations

import sqlite3
from hashlib import sha1
from pathlib import Path
from typing import Any

import pytest

from anki_cli.db.anki_direct import AnkiDirectReadStore, DuplicateNoteError
from tests.integration.conftest import COL_TABLE_SQL, insert_col_row


def _checksum(first_field: str) -> int:
    digest = sha1(first_field.encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _make_store(tmp_path: Path) -> tuple[AnkiDirectReadStore, Path]:
    db_path = tmp_path / "collection.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE decks (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        );

        CREATE TABLE notetypes (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            config BLOB NOT NULL
        );

        CREATE TABLE fields (
            ntid INTEGER NOT NULL,
            ord INTEGER NOT NULL,
            name TEXT NOT NULL
        );

        CREATE TABLE templates (
            ntid INTEGER NOT NULL,
            ord INTEGER NOT NULL,
            config BLOB NOT NULL DEFAULT X''
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

        CREATE TABLE graves (
            oid INTEGER NOT NULL,
            type INTEGER NOT NULL,
            usn INTEGER NOT NULL,
            PRIMARY KEY (oid, type)
        );
        """)
    conn.executescript(COL_TABLE_SQL)
    insert_col_row(conn, crt=0)
    conn.executemany(
        "INSERT INTO decks (id, name) VALUES (?, ?)",
        [(1, "Default"), (2, "Other")],
    )
    conn.execute(
        "INSERT INTO notetypes (id, name, config) VALUES (?, ?, ?)",
        (10, "Basic", b""),
    )
    conn.executemany(
        "INSERT INTO fields (ntid, ord, name) VALUES (?, ?, ?)",
        [(10, 0, "Front"), (10, 1, "Back")],
    )
    conn.commit()
    conn.close()

    return AnkiDirectReadStore(db_path), db_path


def _insert_note(
    db_path: Path,
    *,
    note_id: int,
    front: str,
    back: str,
    tags: str = " old ",
) -> None:
    flds = f"{front}\x1f{back}"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO notes (id, guid, mid, mod, usn, tags, flds, sfld, csum, flags, data)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '')
        """,
        (
            note_id,
            f"guid-{note_id}",
            10,
            1,
            -1,
            tags,
            flds,
            front,
            _checksum(front),
        ),
    )
    conn.commit()
    conn.close()


def _insert_card(
    db_path: Path,
    *,
    card_id: int,
    note_id: int,
    deck_id: int = 1,
    ord_: int = 0,
    queue: int = 0,
    due: int = 0,
    card_type: int = 0,
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO cards (
            id, nid, did, ord, mod, usn, type, queue, due, ivl, factor, reps,
            lapses, left, odue, odid, flags, data
        )
        VALUES (?, ?, ?, ?, ?, -1, ?, ?, ?, 0, 0, 0, 0, 0, 0, 0, 0, '{}')
        """,
        (card_id, note_id, deck_id, ord_, 1, card_type, queue, due),
    )
    conn.commit()
    conn.close()


def _note_row(db_path: Path, note_id: int) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, mid, mod, usn, tags, flds, sfld, csum FROM notes WHERE id = ?",
        (note_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    return dict(row)


def _cards_for_note(db_path: Path, note_id: int) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, nid, did, ord, type, queue, due, ivl, factor, reps, lapses, left, usn
        FROM cards
        WHERE nid = ?
        ORDER BY id
        """,
        (note_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def _note_ids(db_path: Path) -> list[int]:
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT id FROM notes ORDER BY id").fetchall()
    conn.close()
    return [int(row[0]) for row in rows]


def _card_ids(db_path: Path) -> list[int]:
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT id FROM cards ORDER BY id").fetchall()
    conn.close()
    return [int(row[0]) for row in rows]


def _grave_rows(db_path: Path) -> list[tuple[int, int, int]]:
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT oid, type, usn FROM graves").fetchall()
    conn.close()
    return [(int(oid), int(gtype), int(usn)) for (oid, gtype, usn) in rows]


def test_add_note_creates_note_and_card_with_ordered_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    # Existing new card sets max due to 7, so inserted card should be due 8.
    _insert_note(db_path, note_id=500, front="OldFront", back="OldBack")
    _insert_card(db_path, card_id=700, note_id=500, queue=0, due=7)

    note_id = store.add_note(
        deck="Default",
        notetype="Basic",
        fields={"Back": "A", "Front": "Q"},
        tags=["zeta", "alpha", "alpha"],
        allow_duplicate=False,
    )

    note = _note_row(db_path, note_id)
    assert note["mid"] == 10
    assert note["flds"] == "Q\x1fA"  # schema order: Front, Back
    assert note["sfld"] == "Q"
    assert note["csum"] == _checksum("Q")
    assert note["tags"] == " alpha zeta "
    assert note["usn"] == -1

    cards = _cards_for_note(db_path, note_id)
    assert len(cards) == 1
    assert cards[0]["did"] == 1
    assert cards[0]["ord"] == 0
    assert cards[0]["type"] == 0
    assert cards[0]["queue"] == 0
    assert cards[0]["due"] == 8
    assert cards[0]["usn"] == -1


def test_add_note_missing_required_field_raises_lookup_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    with pytest.raises(LookupError, match="Missing field 'Back'"):
        store.add_note(
            deck="Default",
            notetype="Basic",
            fields={"Front": "Q"},
            tags=None,
            allow_duplicate=False,
        )

    assert _note_ids(db_path) == []


def test_update_note_updates_fields_tags_and_checksum(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    _insert_note(db_path, note_id=1001, front="F0", back="B0", tags=" old ")

    result = store.update_note(
        note_id=1001,
        fields={"Back": "B1", "Front": "F1"},
        tags=["z", "a"],
    )

    assert result == {
        "note_id": 1001,
        "updated_fields": True,
        "updated_tags": True,
        "generated_cards": [],
    }

    note = _note_row(db_path, 1001)
    assert note["flds"] == "F1\x1fB1"
    assert note["sfld"] == "F1"
    assert note["csum"] == _checksum("F1")
    assert note["tags"] == " a z "
    assert note["usn"] == -1


def test_update_note_unknown_field_raises_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    _insert_note(db_path, note_id=1002, front="F0", back="B0", tags=" old ")
    before = _note_row(db_path, 1002)

    with pytest.raises(LookupError, match="does not exist"):
        store.update_note(note_id=1002, fields={"Nope": "x"}, tags=None)

    after = _note_row(db_path, 1002)
    assert after == before


def test_delete_notes_deletes_existing_tracks_missing_and_writes_graves(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    _insert_note(db_path, note_id=1001, front="A", back="B")
    _insert_note(db_path, note_id=1002, front="C", back="D")
    _insert_card(db_path, card_id=2001, note_id=1001)
    _insert_card(db_path, card_id=2002, note_id=1001)
    _insert_card(db_path, card_id=2003, note_id=1002)

    result = store.delete_notes([1001, 9999, 1001, 0, -5])

    assert result == {
        "requested": 2,  # normalized positive unique IDs: [1001, 9999]
        "deleted_notes": 1,
        "deleted_cards": 2,
        "missing_note_ids": [9999],
    }

    assert _note_ids(db_path) == [1002]
    assert _card_ids(db_path) == [2003]
    assert set(_grave_rows(db_path)) == {
        (2001, 0, -1),
        (2002, 0, -1),
        (1001, 1, -1),
    }


def test_delete_notes_empty_or_non_positive_input_returns_noop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, _db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    assert store.delete_notes([]) == {
        "requested": 0,
        "deleted_notes": 0,
        "deleted_cards": 0,
        "missing_note_ids": [],
    }
    assert store.delete_notes([0, -1, -9]) == {
        "requested": 0,
        "deleted_notes": 0,
        "deleted_cards": 0,
        "missing_note_ids": [],
    }


# --- card generation honours the templates (#22 item 7) ---------------------------


def _install_templates(db_path: Path, ntid: int, fronts: list[str]) -> None:
    from anki_cli.proto.anki.notetypes import NotetypeTemplateConfig

    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM templates WHERE ntid = ?", (ntid,))
    for ord_, front in enumerate(fronts):
        conn.execute(
            "INSERT INTO templates (ntid, ord, config) VALUES (?, ?, ?)",
            (ntid, ord_, bytes(NotetypeTemplateConfig(q_format=front, a_format="{{FrontSide}}"))),
        )
    conn.commit()
    conn.close()


def _card_ords(db_path: Path, note_id: int) -> list[int]:
    return sorted(int(c["ord"]) for c in _cards_for_note(db_path, note_id))


def test_add_note_basic_and_reversed_with_empty_back_makes_one_card(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression for #22 (7): 'Basic (and reversed card)' with an empty Back must
    not generate the reverse card; pre-fix every template produced a card."""
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    _install_templates(db_path, 10, ["{{Front}}", "{{Back}}"])

    one = store.add_note(
        deck="Default",
        notetype="Basic",
        fields={"Front": "Q", "Back": ""},
        tags=[],
        allow_duplicate=True,
    )
    both = store.add_note(
        deck="Default",
        notetype="Basic",
        fields={"Front": "Q2", "Back": "A"},
        tags=[],
        allow_duplicate=True,
    )

    assert _card_ords(db_path, one) == [0]
    assert _card_ords(db_path, both) == [0, 1]


def test_add_note_whitespace_or_br_only_field_counts_as_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    _install_templates(db_path, 10, ["{{Front}}", "{{Back}}"])

    nid = store.add_note(
        deck="Default",
        notetype="Basic",
        fields={"Front": "Q", "Back": " <br>\n<div></div>"},
        tags=[],
        allow_duplicate=True,
    )
    img = store.add_note(
        deck="Default",
        notetype="Basic",
        fields={"Front": "Q3", "Back": '<img src="x.png">'},
        tags=[],
        allow_duplicate=True,
    )

    assert _card_ords(db_path, nid) == [0]
    assert _card_ords(db_path, img) == [0, 1]  # media is content


def test_add_note_with_nothing_renderable_still_gets_the_first_card(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """rslib ensure_not_empty: a brand-new note always gets template 0."""
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    _install_templates(db_path, 10, ["{{Front}}", "{{Back}}"])

    nid = store.add_note(
        deck="Default",
        notetype="Basic",
        fields={"Front": "", "Back": ""},
        tags=[],
        allow_duplicate=True,
    )

    assert _card_ords(db_path, nid) == [0]


def test_add_note_conditional_front_and_special_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    _install_templates(
        db_path,
        10,
        [
            "{{Front}}",
            "{{#Back}}{{Front}}{{/Back}}",  # only when Back is filled
            "{{Tags}}",  # special field: only when the note has tags
            "{{Deck}}",  # special field: always available
        ],
    )

    no_tags = store.add_note(
        deck="Default",
        notetype="Basic",
        fields={"Front": "Q", "Back": ""},
        tags=[],
        allow_duplicate=True,
    )
    tagged = store.add_note(
        deck="Default",
        notetype="Basic",
        fields={"Front": "Q2", "Back": "A"},
        tags=["t"],
        allow_duplicate=True,
    )

    assert _card_ords(db_path, no_tags) == [0, 3]
    assert _card_ords(db_path, tagged) == [0, 1, 2, 3]


def test_update_note_generates_cards_whose_template_now_renders(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """rslib generate_cards_for_existing_note: filling Back later must create the
    reverse card that add_note correctly withheld."""
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    _install_templates(db_path, 10, ["{{Front}}", "{{Back}}"])
    nid = store.add_note(
        deck="Other",
        notetype="Basic",
        fields={"Front": "Q", "Back": ""},
        tags=[],
        allow_duplicate=True,
    )
    assert _card_ords(db_path, nid) == [0]

    result = store.update_note(note_id=nid, fields={"Back": "A"}, tags=None)

    assert result["generated_cards"] == [1]
    cards = _cards_for_note(db_path, nid)
    assert sorted(int(c["ord"]) for c in cards) == [0, 1]
    # New card lands in the deck of the note's existing cards, as a new card.
    new = next(c for c in cards if int(c["ord"]) == 1)
    assert (new["did"], new["type"], new["queue"]) == (2, 0, 0)


def test_update_note_never_removes_cards_and_reports_none_when_nothing_new(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Emptying a field does not delete its card (Anki leaves that to Empty Cards)."""
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    _install_templates(db_path, 10, ["{{Front}}", "{{Back}}"])
    nid = store.add_note(
        deck="Default",
        notetype="Basic",
        fields={"Front": "Q", "Back": "A"},
        tags=[],
        allow_duplicate=True,
    )

    result = store.update_note(note_id=nid, fields={"Back": ""}, tags=None)

    assert result["generated_cards"] == []
    assert _card_ords(db_path, nid) == [0, 1]


def test_update_note_tags_can_unlock_a_tags_template(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    _install_templates(db_path, 10, ["{{Front}}", "{{Tags}}"])
    # Whitespace-only tags normalize to none: no Tags card yet.
    nid = store.add_note(
        deck="Default",
        notetype="Basic",
        fields={"Front": "Q", "Back": ""},
        tags=[" "],
        allow_duplicate=True,
    )
    assert _card_ords(db_path, nid) == [0]

    result = store.update_note(note_id=nid, fields=None, tags=["t"])

    assert result["generated_cards"] == [1]
    assert _card_ords(db_path, nid) == [0, 1]


# --- duplicate detection (#23) -------------------------------------------------


def _add_basic(store: AnkiDirectReadStore, front: str, *, allow_duplicate: bool) -> int:
    return store.add_note(
        deck="Default",
        notetype="Basic",
        fields={"Front": front, "Back": "A"},
        tags=None,
        allow_duplicate=allow_duplicate,
    )


def test_add_note_rejects_duplicate_first_field_in_same_notetype(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    _insert_note(db_path, note_id=500, front="hola", back="hello")

    with pytest.raises(DuplicateNoteError) as excinfo:
        _add_basic(store, "hola", allow_duplicate=False)

    assert excinfo.value.duplicate_ids == [500]
    assert excinfo.value.notetype == "Basic"
    assert "500" in str(excinfo.value)
    assert "--allow-duplicate" in str(excinfo.value)
    # Nothing was written: no note, no card.
    assert _note_ids(db_path) == [500]
    assert _card_ids(db_path) == []


def test_add_note_duplicate_is_a_value_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    _insert_note(db_path, note_id=500, front="hola", back="hello")

    with pytest.raises(ValueError, match="Duplicate note"):
        _add_basic(store, "hola", allow_duplicate=False)


def test_add_note_allow_duplicate_inserts_second_note(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    _insert_note(db_path, note_id=500, front="hola", back="hello")

    nid = _add_basic(store, "hola", allow_duplicate=True)

    assert _note_ids(db_path) == [500, nid]
    assert _note_row(db_path, nid)["csum"] == _note_row(db_path, 500)["csum"]


def test_add_note_duplicate_check_is_scoped_to_notetype(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    # Same first field already exists, but under a different notetype (mid 20).
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO notetypes (id, name, config) VALUES (?, ?, ?)", (20, "Cloze-ish", b"")
    )
    conn.execute("INSERT INTO fields (ntid, ord, name) VALUES (?, ?, ?)", (20, 0, "Text"))
    conn.execute(
        """
        INSERT INTO notes (id, guid, mid, mod, usn, tags, flds, sfld, csum, flags, data)
        VALUES (?, ?, ?, 1, -1, '', ?, ?, ?, 0, '')
        """,
        (600, "guid-600", 20, "hola", "hola", store._field_checksum("hola")),
    )
    conn.commit()
    conn.close()

    nid = _add_basic(store, "hola", allow_duplicate=False)

    assert _note_row(db_path, nid)["mid"] == 10
    assert _note_ids(db_path) == [600, nid]


def test_add_note_empty_first_field_is_never_a_duplicate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    _install_templates(db_path, 10, ["{{Front}}{{Back}}"])
    _insert_note(db_path, note_id=500, front="", back="first")

    nid = store.add_note(
        deck="Default",
        notetype="Basic",
        fields={"Front": "  ", "Back": "second"},
        tags=None,
        allow_duplicate=False,
    )

    assert _note_ids(db_path) == [500, nid]


def test_add_note_computes_checksum_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, _ = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    calls: list[str] = []
    real = store._field_checksum

    def counting(first_field: str) -> int:
        calls.append(first_field)
        return real(first_field)

    monkeypatch.setattr(store, "_field_checksum", counting)

    _add_basic(store, "Q", allow_duplicate=False)

    assert calls == ["Q"]


def test_add_notes_bulk_reports_duplicate_as_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    _insert_note(db_path, note_id=500, front="hola", back="hello")

    out = store.add_notes(
        [
            {"deck": "Default", "notetype": "Basic", "fields": {"Front": "hola", "Back": "x"}},
            {"deck": "Default", "notetype": "Basic", "fields": {"Front": "adios", "Back": "y"}},
        ]
    )

    assert out[0] is None
    assert isinstance(out[1], int)
    assert _note_ids(db_path) == [500, out[1]]
