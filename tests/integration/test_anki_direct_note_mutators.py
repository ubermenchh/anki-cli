from __future__ import annotations

import time
from hashlib import sha1
from pathlib import Path
from typing import Any

import pytest

from anki_cli.backends.protocol import JSONValue
from anki_cli.db.anki_direct import (
    AnkiDirectReadStore,
    DuplicateNoteError,
    EmptyNoteError,
    NoteRejectedError,
)
from tests.anki_schema import connect
from tests.conftest import COL_BASE_MOD_MS, Collection, new_collection
from tests.integration.conftest import (
    CSUM_A_SPACE_B,
    CSUM_AMPX_KEPT,
    CSUM_AUDIO_A_MP3,
    CSUM_BAD_NUM_KEPT,
    CSUM_BOGUS_KEPT,
    CSUM_CAPITAL_A,
    CSUM_CHECK_KEPT,
    CSUM_F1,
    CSUM_HEX_UPPER_X_KEPT,
    CSUM_IMG_X_PNG,
    CSUM_IMG_Y_JPG,
    CSUM_IMG_Z_GIF,
    CSUM_KYOU,
    CSUM_NBSP_NOSEMI_KEPT,
    CSUM_OBJECT_O_SWF,
    CSUM_Q,
    CSUM_Q_AMP_A,
    CSUM_R_AMPD_KEPT,
    CSUM_SURROGATE_KEPT,
    CSUM_TEST,
    CSUM_VIDEO_V_MP4,
    assert_col_untouched,
)


def _checksum(first_field: str) -> int:
    """Raw sha1-based csum of the field *as written* (no HTML stripping).

    Fixture helper only — the ``CSUM_*`` literals are the oracle for the
    store's own ``_field_checksum``.
    """
    digest = sha1(first_field.encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _make_store(tmp_path: Path) -> tuple[AnkiDirectReadStore, Path]:
    """Decks Default/Other, notetype 10 "Basic" (Front/Back) with **no
    templates**: add_note then relies on the ensure_not_empty fallback (one
    card, ord 0), which is what these tests were written against. Tests that
    need real templates call ``_install_templates``."""
    col = new_collection(tmp_path / "collection.anki2", seed=False)
    col.insert_deck(id=1, name="Default")
    col.insert_deck(id=2, name="Other")
    col.insert_notetype(id=10, name="Basic", fields=["Front", "Back"], templates=[])
    return col.store(writable=False), col.db_path


def _insert_note(
    db_path: Path,
    *,
    note_id: int,
    front: str,
    back: str,
    tags: str = " old ",
    csum: int = 0,
) -> None:
    # ``csum`` is fixture input, never asserted — the literal-oracle tests own
    # the real values. ``0`` just keeps the column populated. Rows start
    # *synced* (usn 0) so a mutator's re-flag to -1 is observable.
    Collection(db_path).insert_note(
        id=note_id, fields=[front, back], tags=tags, mod=1, usn=0, sfld=front, csum=csum
    )


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
    Collection(db_path).insert_card(
        id=card_id, nid=note_id, did=deck_id, ord=ord_, mod=1, usn=0, type=card_type,
        queue=queue, due=due,
    )


def _note_row(db_path: Path, note_id: int) -> dict[str, Any]:
    conn = connect(str(db_path))
    row = conn.execute(
        "SELECT id, mid, mod, usn, tags, flds, sfld, csum FROM notes WHERE id = ?",
        (note_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    return dict(row)


def _cards_for_note(db_path: Path, note_id: int) -> list[dict[str, Any]]:
    conn = connect(str(db_path))
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
    conn = connect(str(db_path))
    rows = conn.execute("SELECT id FROM notes ORDER BY id").fetchall()
    conn.close()
    return [int(row[0]) for row in rows]


def _card_ids(db_path: Path) -> list[int]:
    conn = connect(str(db_path))
    rows = conn.execute("SELECT id FROM cards ORDER BY id").fetchall()
    conn.close()
    return [int(row[0]) for row in rows]


def _grave_rows(db_path: Path) -> list[tuple[int, int, int]]:
    conn = connect(str(db_path))
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
    assert note["csum"] == CSUM_Q
    assert note["tags"] == " alpha zeta "
    assert note["usn"] == -1

    cards = _cards_for_note(db_path, note_id)
    assert len(cards) == 1
    assert cards[0]["did"] == 1
    assert cards[0]["ord"] == 0
    assert cards[0]["type"] == 0
    assert cards[0]["queue"] == 0
    assert cards[0]["due"] == 8
    # Note and card both flagged for sync, and col.mod moved so sync looks (#47).
    Collection(db_path).assert_synced("notes", note_id)
    Collection(db_path).assert_synced("cards", int(cards[0]["id"]))


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
    assert note["csum"] == CSUM_F1
    assert note["tags"] == " a z "
    Collection(db_path).assert_synced("notes", 1001)


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
    assert Collection(db_path).col()["mod"] > COL_BASE_MOD_MS  # graves must sync


def test_delete_notes_empty_or_non_positive_input_returns_noop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
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
    Collection(db_path).assert_untouched()  # a no-op must not dirty col.mod


@pytest.mark.parametrize(
    ("first_field", "expected"),
    [
        # External oracle: rslib's own vectors (rslib/src/notes/mod.rs).
        ("test", CSUM_TEST),
        ("今日", CSUM_KYOU),
        # Plain text and markup-stripped equivalents.
        ("Q", CSUM_Q),
        ("<b>Q</b>", CSUM_Q),
        # Media tags keep their src/data filename: every tag name branch,
        # case-folded, double/single-quoted and unquoted attribute values.
        ('<img src="x.png">', CSUM_IMG_X_PNG),
        ("<IMG SRC='y.jpg'>", CSUM_IMG_Y_JPG),
        ("<img src=z.gif>", CSUM_IMG_Z_GIF),
        ('<audio src="a.mp3"></audio>', CSUM_AUDIO_A_MP3),
        ('<object data="o.swf">', CSUM_OBJECT_O_SWF),
        ('<video src="v.mp4"></video>', CSUM_VIDEO_V_MP4),
        # comment / style / script alternations.
        ("<!-- c -->Q", CSUM_Q),
        ("<style>p{}</style>Q", CSUM_Q),
        ("<script>x()</script>Q", CSUM_Q),
        # Mutation survivors: attributed <style>, a newline inside a comment
        # and inside a media tag — each kills a mutant (dropped ``re.DOTALL``,
        # narrowed ``<script[^>]*>``, ``video``/``source`` cut from the
        # alternation) that the rows above all pass.
        ('<style type="text/css">\np{}\n</style>Q', CSUM_Q),
        ("<!-- multi\nline -->Q", CSUM_Q),
        ('<img\n  src="x.png">', CSUM_IMG_X_PNG),
        # Entity paths: well-formed entities decode.
        ("Q&amp;A", CSUM_Q_AMP_A),
        ("a&nbsp;b", CSUM_A_SPACE_B),
        ("&#65;", CSUM_CAPITAL_A),
        ("&#x41;", CSUM_CAPITAL_A),
        # Strict all-or-nothing decode: any malformed entity keeps the ENTIRE
        # original string (htmlescape errs → rslib hashes the input verbatim).
        ("R&amp;D & more", CSUM_R_AMPD_KEPT),  # bare '&' → PrematureEnd
        ("&ampx", CSUM_AMPX_KEPT),  # no-semicolon legacy form → PrematureEnd
        ("a&nbsp b", CSUM_NBSP_NOSEMI_KEPT),  # no-semicolon legacy form
        ("&check;", CSUM_CHECK_KEPT),  # HTML5-only name → UnknownEntity
        ("&#X41;", CSUM_HEX_UPPER_X_KEPT),  # uppercase X → MalformedNumEscape
        ("&#xD800;", CSUM_SURROGATE_KEPT),  # surrogate → InvalidCharacter
        ("&bogus;", CSUM_BOGUS_KEPT),  # unknown name → UnknownEntity
        ("&#32a;", CSUM_BAD_NUM_KEPT),  # junk digit → MalformedNumEscape
    ],
)
def test_field_checksum_matches_anki(
    first_field: str,
    expected: int,
    tmp_path: Path,
) -> None:
    """Pin ``_field_checksum`` to the Anki/rslib oracle.

    Anki strips HTML before hashing but keeps media filenames, so the literal
    for ``<b>Q</b>`` equals the one for ``Q`` — the old oracle re-implemented
    the hash line-for-line and could not detect that divergence. ``"test"``
    and ``"今日"`` are rslib's own test vectors; the rest pin literals for the
    stripped text named in conftest.
    """
    db_path = tmp_path / "collection.db"
    db_path.touch()  # pure function — only the path must exist
    store = AnkiDirectReadStore(db_path)

    assert store._field_checksum(first_field) == expected


def test_field_checksum_srcless_media_tag_is_linear(tmp_path: Path) -> None:
    """Regression for the regex backtracking bug.

    A media tag whose quoted attributes never reach ``src=``/``data=`` made the
    old ``(?:[^>]|"[^"]+?"|'[^']+?')+?`` enumerate 2**n parses; 40 attrs must
    strip to ``"Q"`` in well under a second even while the write lock is held.
    """
    attrs = " ".join(f'a{i}="v{i}"' for i in range(40))
    db_path = tmp_path / "collection.db"
    db_path.touch()
    store = AnkiDirectReadStore(db_path)

    t0 = time.perf_counter()
    assert store._field_checksum(f"<img {attrs}>Q") == CSUM_Q
    assert time.perf_counter() - t0 < 0.5


def test_add_note_rejects_csum_duplicate_across_markup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``<b>Q</b>`` must collide with ``Q`` — the point of stripping for csum.

    The refusal is an ordinary ``DuplicateNoteError`` (csum is compared, not
    markup), the refusal persists nothing, and ``allow_duplicate=True`` still
    inserts — the flag lifts the check, not the checksum.
    """
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    _insert_note(db_path, note_id=1, front="Q", back="A", csum=CSUM_Q)

    with pytest.raises(DuplicateNoteError):
        store.add_note(
            deck="Default",
            notetype="Basic",
            fields={"Front": "<b>Q</b>", "Back": "A"},
            tags=None,
            allow_duplicate=False,
        )
    assert _note_ids(db_path) == [1]

    second = store.add_note(
        deck="Default",
        notetype="Basic",
        fields={"Front": "<b>Q</b>", "Back": "A"},
        tags=None,
        allow_duplicate=True,
    )
    assert _note_ids(db_path) == [1, second]


def test_add_note_checksums_stripped_first_field(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """End-to-end: a markup-bearing first field still lands the Anki csum."""
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    note_id = store.add_note(
        deck="Default",
        notetype="Basic",
        fields={"Back": "A", "Front": "<b>Q</b>"},
        tags=None,
        allow_duplicate=False,
    )

    note = _note_row(db_path, note_id)
    # Anki stores the *stripped* sort field alongside the stripped checksum.
    assert note["sfld"] == "Q"
    assert note["flds"] == "<b>Q</b>\x1fA"  # the field itself keeps its markup
    assert note["csum"] == CSUM_Q


# --- card generation honours the templates (#22 item 7) ---------------------------


def _install_templates(db_path: Path, ntid: int, fronts: list[str]) -> None:
    col = Collection(db_path)
    col.execute("DELETE FROM templates WHERE ntid = ?", (ntid,))
    for ord_, front in enumerate(fronts):
        col.insert_template(
            ntid=ntid, ord=ord_, name=f"Card {ord_ + 1}", front=front, back="{{FrontSide}}"
        )


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
    """rslib ensure_not_empty: a brand-new note always gets template 0.

    An empty first field is refused outright (``EmptyNoteError``), so "nothing
    renderable" is reached with a filled first field that no template uses.
    """
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    _install_templates(db_path, 10, ["{{Back}}", "{{#Back}}x{{/Back}}"])

    nid = store.add_note(
        deck="Default",
        notetype="Basic",
        fields={"Front": "Q", "Back": ""},
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


# --- duplicate / empty rejection (#23) ------------------------------------------
#
# Duplicate seeds go through the real ``add_note`` (``allow_duplicate=True``) rather
# than ``_insert_note`` so the seed's csum is whatever the implementation computes;
# the tests then hold regardless of how the fixture helper populates ``csum``.


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
    first = _add_basic(store, "hola", allow_duplicate=True)
    cards_before = _card_ids(db_path)
    assert len(cards_before) == 1  # a success writes a card, so the assertion below has teeth

    with pytest.raises(DuplicateNoteError) as excinfo:
        _add_basic(store, "hola", allow_duplicate=False)

    assert excinfo.value.duplicate_ids == [first]
    assert excinfo.value.notetype == "Basic"
    assert str(first) in str(excinfo.value)
    # The store states the fact; the CLI appends the --allow-duplicate remedy.
    assert "--allow-duplicate" not in str(excinfo.value)
    # Nothing persisted: no note, no card.
    assert _note_ids(db_path) == [first]
    assert _card_ids(db_path) == cards_before


def test_add_note_duplicate_refusal_leaves_col_untouched(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The refusal happens before any write, so ``col.mod`` must not move (#47)."""
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    # Seed via SQL so the seed itself does not bump col.mod.
    _insert_note(db_path, note_id=500, front="hola", back="hello")
    conn = connect(str(db_path))
    conn.execute("UPDATE notes SET csum = ? WHERE id = 500", (store._field_checksum("hola"),))
    conn.commit()
    conn.close()

    with pytest.raises(DuplicateNoteError):
        _add_basic(store, "hola", allow_duplicate=False)

    assert_col_untouched(db_path)


def test_add_note_duplicate_is_a_value_error_and_a_rejection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, _ = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    _add_basic(store, "hola", allow_duplicate=True)

    with pytest.raises(NoteRejectedError):
        _add_basic(store, "hola", allow_duplicate=False)
    with pytest.raises(ValueError, match="Duplicate note"):
        _add_basic(store, "hola", allow_duplicate=False)


def test_add_note_allow_duplicate_inserts_second_note(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    first = _add_basic(store, "hola", allow_duplicate=True)

    second = _add_basic(store, "hola", allow_duplicate=True)

    assert _note_ids(db_path) == [first, second]
    assert _note_row(db_path, second)["csum"] == _note_row(db_path, first)["csum"]


def test_add_note_reports_every_duplicate_id_ascending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """All matches, oldest first — pins the absence of a LIMIT and the sort order."""
    store, _ = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    a = _add_basic(store, "hola", allow_duplicate=True)
    b = _add_basic(store, "hola", allow_duplicate=True)

    with pytest.raises(DuplicateNoteError) as excinfo:
        _add_basic(store, "hola", allow_duplicate=False)

    assert excinfo.value.duplicate_ids == [a, b]
    assert str(a) in str(excinfo.value) and str(b) in str(excinfo.value)


def test_add_note_duplicate_check_is_scoped_to_notetype(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    # Same first field already exists, but under a different notetype (mid 20).
    col = Collection(db_path)
    col.insert_notetype(id=20, name="Cloze-ish", fields=["Text"], templates=[])
    col.insert_note(
        id=600, mid=20, fields=["hola"], mod=1, usn=-1, csum=store._field_checksum("hola")
    )

    nid = _add_basic(store, "hola", allow_duplicate=False)

    assert _note_row(db_path, nid)["mid"] == 10
    assert _note_ids(db_path) == [600, nid]


@pytest.mark.parametrize(
    "front",
    ["", "   ", "<br>", "<div></div>", "<br/> \n <div><br></div>"],
    ids=["empty", "whitespace", "br", "empty-div", "markup-soup"],
)
def test_add_note_rejects_empty_first_field(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    front: str,
) -> None:
    """AnkiConnect: "cannot create note because it is empty" (rslib Empty state).

    Uses the same ``field_is_empty`` predicate as card generation, so ``<br>``
    is empty here too. Not lifted by ``allow_duplicate``.
    """
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    _install_templates(db_path, 10, ["{{Front}}{{Back}}"])

    for allow in (False, True):
        with pytest.raises(EmptyNoteError) as excinfo:
            store.add_note(
                deck="Default",
                notetype="Basic",
                fields={"Front": front, "Back": "second"},
                tags=None,
                allow_duplicate=allow,
            )
        assert excinfo.value.field_name == "Front"
        assert excinfo.value.notetype == "Basic"
        assert isinstance(excinfo.value, NoteRejectedError)

    assert _note_ids(db_path) == []
    assert _card_ids(db_path) == []
    assert_col_untouched(db_path)


def test_add_note_empty_check_runs_before_duplicate_check(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """rslib note_fields_check orders Empty before Duplicate; an empty front is
    reported as empty even if an identical empty-front row already exists."""
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    _insert_note(db_path, note_id=500, front="   ", back="legacy")
    conn = connect(str(db_path))
    conn.execute("UPDATE notes SET csum = ? WHERE id = 500", (store._field_checksum("   "),))
    conn.commit()
    conn.close()

    with pytest.raises(EmptyNoteError):
        _add_basic(store, "   ", allow_duplicate=False)


def test_add_notes_bulk_reports_per_item_refusals_as_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Duplicate, empty and missing-deck items come back as ``None`` (AnkiConnect
    ``addNotes`` shape); the good item still lands."""
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    first = _add_basic(store, "hola", allow_duplicate=True)

    out = store.add_notes(
        [
            {"deck": "Default", "notetype": "Basic", "fields": {"Front": "hola", "Back": "x"}},
            {"deck": "Default", "notetype": "Basic", "fields": {"Front": "<br>", "Back": "x"}},
            {"deck": "Nope", "notetype": "Basic", "fields": {"Front": "adios", "Back": "y"}},
            {"deck": "Default", "notetype": "Basic", "fields": {"Front": "adios", "Back": "y"}},
        ]
    )

    assert out[:3] == [None, None, None]
    assert isinstance(out[3], int)
    assert _note_ids(db_path) == [first, out[3]]


def test_add_notes_bulk_propagates_collection_level_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failure that would hit every item the same way (here: the write guard
    refusing because Anki is open) must fail the whole call, not surface as N
    ``None`` entries that read like N duplicates."""
    store, db_path = _make_store(tmp_path)

    def _anki_is_open() -> None:
        raise RuntimeError("Anki Desktop appears to be running")

    monkeypatch.setattr(store, "_ensure_write_safe", _anki_is_open)

    with pytest.raises(RuntimeError, match="Anki Desktop"):
        store.add_notes(
            [
                {"deck": "Default", "notetype": "Basic", "fields": {"Front": "a", "Back": "x"}},
                {"deck": "Default", "notetype": "Basic", "fields": {"Front": "b", "Back": "y"}},
            ]
        )

    assert _note_ids(db_path) == []


def test_add_notes_bulk_second_identical_item_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Each item commits on its own, so a repeat *within* the batch is a duplicate of
    the item before it — the shape a re-run import file produces."""
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    item: dict[str, JSONValue] = {
        "deck": "Default",
        "notetype": "Basic",
        "fields": {"Front": "hola", "Back": "x"},
    }

    out = store.add_notes([item, dict(item)])

    assert isinstance(out[0], int)
    assert out[1] is None
    assert _note_ids(db_path) == [out[0]]


def test_add_notes_bulk_allow_duplicate_inserts_repeats(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    first = _add_basic(store, "hola", allow_duplicate=True)
    item: dict[str, JSONValue] = {
        "deck": "Default",
        "notetype": "Basic",
        "fields": {"Front": "hola", "Back": "x"},
    }

    out = store.add_notes([item, dict(item)], allow_duplicate=True)

    assert all(isinstance(i, int) for i in out)
    assert _note_ids(db_path) == [first, *out]


def test_add_notes_bulk_allow_duplicate_does_not_lift_empty_refusal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    out = store.add_notes(
        [{"deck": "Default", "notetype": "Basic", "fields": {"Front": "<br>", "Back": "x"}}],
        allow_duplicate=True,
    )

    assert out == [None]
    assert _note_ids(db_path) == []


def test_add_note_missing_field_is_reported_before_duplicate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Field validation runs before the duplicate lookup, so a malformed request
    gets the actionable error even when its first field would also collide."""
    store, _ = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    _add_basic(store, "hola", allow_duplicate=True)

    with pytest.raises(LookupError, match="Missing field 'Back'"):
        store.add_note(
            deck="Default",
            notetype="Basic",
            fields={"Front": "hola"},
            tags=None,
            allow_duplicate=False,
        )


def test_duplicate_note_error_message_truncates_long_id_lists() -> None:
    ids = list(range(1, 16))
    err = DuplicateNoteError(notetype="Basic", duplicate_ids=ids)

    assert err.duplicate_ids == ids  # full list always available to callers
    msg = str(err)
    assert "1, 2, 3, 4, 5, 6, 7, 8, 9, 10 and 5 more" in msg
    assert "11" not in msg.split(" and ")[0]


# --- stripped first field: duplicates, emptiness, sfld (#23 items 2/5) ----------


def test_add_note_duplicate_detection_ignores_markup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Anki checksums the *text* of the first field, so a CLI note whose front only
    differs from an existing one by markup is a duplicate — and vice versa."""
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    first = _add_basic(store, "<b>hola</b>", allow_duplicate=True)
    assert _note_row(db_path, first)["sfld"] == "hola"

    for variant in ("hola", "<i>hola</i>", "<div>hola</div>", "hola<br>"):
        with pytest.raises(DuplicateNoteError) as excinfo:
            _add_basic(store, variant, allow_duplicate=False)
        assert excinfo.value.duplicate_ids == [first]

    # Entities decode before hashing too: "Q&amp;A" is "Q&A".
    qa = _add_basic(store, "Q&amp;A", allow_duplicate=True)
    with pytest.raises(DuplicateNoteError) as excinfo:
        _add_basic(store, "Q&A", allow_duplicate=False)
    assert excinfo.value.duplicate_ids == [qa]

    # Different visible text is not a duplicate even with matching markup.
    assert isinstance(_add_basic(store, "<b>adios</b>", allow_duplicate=False), int)


def test_add_note_csum_collision_is_not_a_duplicate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """rslib ``is_duplicate`` confirms every csum hit by comparing the stripped
    first field; a 32-bit collision alone must not refuse the note."""
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    # Seed a note whose stored csum equals hash("hola") but whose text differs.
    _insert_note(db_path, note_id=500, front="something else", back="x",
                 csum=store._field_checksum("hola"))

    nid = _add_basic(store, "hola", allow_duplicate=False)

    assert _note_ids(db_path) == [500, nid]
    # And a real match with the same csum is still caught.
    with pytest.raises(DuplicateNoteError) as excinfo:
        _add_basic(store, "hola", allow_duplicate=False)
    assert excinfo.value.duplicate_ids == [nid]  # 500 excluded: text differs


@pytest.mark.parametrize(
    "front",
    ["<b></b>", "&nbsp;", "<span> &nbsp; </span>", "<i><br></i>"],
    ids=["empty-bold", "nbsp", "nbsp-in-span", "br-in-italic"],
)
def test_add_note_first_field_empty_after_stripping_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    front: str,
) -> None:
    """rslib note_fields_check strips markup and decodes entities before the
    emptiness test, so these are Empty even though card-gen's raw predicate
    would call some of them non-empty."""
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    with pytest.raises(EmptyNoteError):
        _add_basic(store, front, allow_duplicate=True)

    assert _note_ids(db_path) == []


def test_add_note_media_only_first_field_is_not_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A media reference contributes its filename, so an image-only front is a
    real note (and its sfld is the filename Anki would store)."""
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)

    nid = _add_basic(store, '<img src="x.png">', allow_duplicate=False)

    note = _note_row(db_path, nid)
    assert note["sfld"] == " x.png "
    assert note["csum"] == CSUM_IMG_X_PNG


def test_update_note_writes_stripped_sfld(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    nid = _add_basic(store, "Q", allow_duplicate=True)

    store.update_note(note_id=nid, fields={"Front": "<b>Zebra</b>&nbsp;"}, tags=None)

    note = _note_row(db_path, nid)
    assert note["flds"] == "<b>Zebra</b>&nbsp;\x1fA"
    assert note["sfld"] == "Zebra "
    assert note["csum"] == store._field_checksum("Zebra ")


# --- sync bookkeeping, every note-side mutator (#37 / #47) --------------------------------


@pytest.mark.parametrize(
    ("mutate", "table", "row_id"),
    [
        (lambda s: s.add_note(deck="Default", notetype="Basic", fields={"Front": "N", "Back": "A"},
                              tags=None, allow_duplicate=False), "notes", None),
        (lambda s: s.update_note(note_id=500, fields={"Back": "B2"}, tags=None), "notes", 500),
        (lambda s: s.update_note(note_id=500, fields=None, tags=["x"]), "notes", 500),
        (lambda s: s.add_tags([500], ["t"]), "notes", 500),
        (lambda s: s.remove_tags([500], ["old"]), "notes", 500),
        (lambda s: s.rename_tag(old_tag="old", new_tag="new"), "notes", 500),
        (lambda s: s.add_notes([{"deck": "Default", "notetype": "Basic",
                                 "fields": {"Front": "B1", "Back": "A"}}]), "notes", None),
    ],
    ids=["add_note", "update_fields", "update_tags", "add_tags", "remove_tags", "rename_tag",
         "add_notes"],
)
def test_every_note_mutator_flags_the_row_and_bumps_col_mod(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutate, table, row_id
) -> None:
    """Anki's sync only scans for ``usn = -1`` rows when ``col.mod`` moved (#47);
    a mutator that forgets either is invisible to the next sync. Seeded rows
    start synced (``usn = 0``) so the re-flag is observable."""
    store, db_path = _make_store(tmp_path)
    monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)
    col = Collection(db_path)
    col.insert_note(id=500, fields=["Q", "A"], tags=["old"], mod=1, usn=0)

    result = mutate(store)

    target = row_id
    if target is None:  # created: find the new row
        target = next(nid for nid in col.ids("notes") if nid != 500)
        if isinstance(result, list):
            assert result == [target]
    col.assert_synced(table, target)
