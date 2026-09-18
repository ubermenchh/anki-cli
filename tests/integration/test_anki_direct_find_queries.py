from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import anki_cli.db.anki_direct as direct_mod
from anki_cli.db.anki_direct import AnkiDirectReadStore
from anki_cli.db.search_sql import SearchParseError
from tests.conftest import new_collection

_TYPE_FOR_QUEUE = {0: 0, 1: 1, 2: 2, 3: 1}


def _make_store(
    tmp_path: Path,
    *,
    decks: list[tuple[int, str]],
    notes: list[tuple[int, str, str, int]],
    cards: list[tuple[int, int, int, int, int]],
    col_crt: int = 0,
) -> AnkiDirectReadStore:
    """Rows: decks ``(id, name)``; notes ``(id, tags, flds, mod)``; cards
    ``(id, nid, did, queue, due)``. ``type`` follows ``queue`` the way Anki sets
    it for an unsuspended card (new/learn/review/relearn 0/1/2/3, day-learn is
    type 1, suspended/buried fall back to new); tests needing a specific
    ``(type, queue)`` UPDATE it afterwards."""
    col = new_collection(tmp_path / "collection.anki2", crt=col_crt, seed=False)
    for did, name in decks:
        col.insert_deck(id=did, name=name)
    col.insert_notetype(id=10, name="Basic", fields=["Front", "Back"])
    for nid, tags, flds, mod in notes:
        col.insert_note(id=nid, fields=flds.split("\x1f"), tags=tags, mod=mod)
    for cid, nid, did, queue, due in cards:
        col.insert_card(
            id=cid, nid=nid, did=did, queue=queue, due=due, type=_TYPE_FOR_QUEUE.get(queue, 0)
        )
    return col.store(writable=False)


def _seed_store(tmp_path: Path) -> AnkiDirectReadStore:
    store = _make_store(
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
            (1004, 102, 3, 3, 11),        # day-learn due (day index; now=1_000_000 is day 11)
            (1005, 102, 3, 2, 11),        # review due (with now=1_000_000, crt=0)
            (1006, 103, 1, 2, 12),        # review not due
            (1010, 103, 1, 3, 40),        # day-learn not due (day 40); #19
            (1007, 104, 4, -1, 0),        # suspended
            (1008, 104, 4, -2, 0),        # buried (manual)
            (1009, 103, 1, -3, 0),        # buried (scheduler)
        ],
        col_crt=0,
    )

    conn = sqlite3.connect(str(store.db_path))
    conn.executemany(
        "UPDATE cards SET ivl = ?, reps = ?, lapses = ?, flags = ? WHERE id = ?",
        [
            (3, 6, 1, 3, 1002),
            (20, 15, 2, 3, 1005),
            (7, 2, 0, 1, 1006),
        ],
    )
    conn.commit()
    conn.close()

    return store


def test_find_note_ids_by_tag_and_deck(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)

    assert store.find_note_ids("tag:foo") == [101, 102]
    assert store.find_note_ids('tag:foo deck:"Lang::Spanish"') == [101]


def test_deck_filter_matches_parent_and_children_like_anki(tmp_path: Path) -> None:
    """``deck:Lang`` is ``Lang`` plus every ``Lang::*`` child; a name that merely
    starts with ``Lang`` is not a child."""
    store = _make_store(
        tmp_path,
        decks=[(1, "Lang"), (2, "Lang::Spanish"), (3, "Lang::Spanish::Verbs"), (4, "Language")],
        notes=[(n, " t ", "x\x1fy", 0) for n in (101, 102, 103, 104)],
        cards=[(900 + n, n, n - 100, 0, 0) for n in (101, 102, 103, 104)],
    )

    assert store.find_card_ids("deck:Lang") == [1001, 1002, 1003]
    assert store.find_card_ids("deck:Lang::Spanish") == [1002, 1003]
    assert store.find_card_ids("deck:Language") == [1004]
    assert store.find_card_ids("-deck:Lang") == [1004]
    assert store.find_note_ids("deck:Lang") == [101, 102, 103]
    # ASCII case-insensitive via SQLite LIKE (guards against a swap to = / GLOB).
    assert store.find_card_ids("deck:lang") == [1001, 1002, 1003]
    # rslib special values.
    assert store.find_card_ids("deck:*") == [1001, 1002, 1003, 1004]
    assert store.find_card_ids("-deck:*") == []


def test_deck_filter_follows_cards_visiting_a_filtered_deck(tmp_path: Path) -> None:
    """A card parked in a filtered deck keeps its home in ``odid``; Anki's
    ``deck:Home`` still finds it (rslib ``write_deck``)."""
    store = _make_store(
        tmp_path,
        decks=[(1, "Home"), (2, "Cram")],
        notes=[(101, " t ", "x\x1fy", 0), (102, " t ", "x\x1fy", 0)],
        cards=[(1001, 101, 2, 2, 0), (1002, 102, 2, 2, 0)],
    )
    conn = sqlite3.connect(str(store.db_path))
    conn.execute("UPDATE cards SET odid = 1 WHERE id = 1001")
    conn.commit()
    conn.close()

    assert store.find_card_ids("deck:Home") == [1001]
    assert store.find_card_ids("deck:Cram") == [1001, 1002]
    assert store.find_card_ids("deck:filtered") == [1001]
    assert store.find_note_ids("deck:filtered") == [101]
    with pytest.raises(SearchParseError, match="deck:current is not supported"):
        store.find_card_ids("deck:current")


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

    # The fixture defaults type from queue, so suspended/buried cards are type 0.
    assert store.find_card_ids("is:new") == [1001, 1007, 1008, 1009]
    assert store.find_card_ids("is:new -is:suspended -is:buried") == [1001]
    assert store.find_card_ids("is:learn") == [1002, 1003, 1004, 1010]
    assert store.find_card_ids("is:review") == [1005, 1006]
    assert store.find_card_ids("is:suspended") == [1007]
    # New cards are never "due" (rslib StateKind::Due); 1001 is queue 0.
    assert store.find_card_ids("is:due") == [1002, 1004, 1005]
    assert store.find_card_ids("is:due is:new") == []


def test_is_new_and_is_review_follow_card_type_not_queue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """rslib write_state: New/Review are card types. A suspended new card is
    still is:new; a relearning card (type 3, queue 1) is still is:review and,
    once its step is due, is:due; a preview-queue card (queue 4) is due too."""
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_000_000)
    store = _make_store(
        tmp_path,
        decks=[(1, "Default")],
        notes=[(n, " t ", "a\x1fb", 0) for n in (1, 2, 3, 4, 5)],
        cards=[
            (1, 1, 1, -1, 0),          # suspended, type new
            (2, 2, 1, 1, 999_000),     # relearning: type 3, queue 1, step due
            (3, 3, 1, 2, 11),          # plain review due
            (4, 4, 1, 4, 999_000),     # preview repeat (queue 4), due
            (5, 5, 1, 0, 0),           # new
        ],
    )
    conn = sqlite3.connect(str(store.db_path))
    conn.execute("UPDATE cards SET type = 0 WHERE id = 1")
    conn.execute("UPDATE cards SET type = 3 WHERE id = 2")
    conn.execute("UPDATE cards SET type = 2 WHERE id = 4")
    conn.commit()
    conn.close()

    assert store.find_card_ids("is:new") == [1, 5]
    assert store.find_card_ids("is:new is:suspended") == [1]
    assert store.find_card_ids("is:review") == [2, 3, 4]
    assert store.find_card_ids("is:learn") == [2]
    assert store.find_card_ids("is:due") == [2, 3, 4]


def test_find_card_ids_combines_due_and_deck_filters(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(direct_mod.time, "time", lambda: 1_000_000)
    store = _seed_store(tmp_path)

    assert store.find_card_ids("is:due deck:Lang::French") == [1004, 1005]
    assert store.find_card_ids("is:due deck:Default") == []

def test_find_note_ids_supports_or_and_parentheses(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)

    assert store.find_note_ids("tag:spanish OR tag:bar deck:Default") == [101, 103]
    assert store.find_note_ids("(tag:spanish OR tag:bar) deck:Default") == [103]


def test_find_card_ids_supports_or_and_parentheses(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)

    assert store.find_card_ids("tag:spanish OR tag:french is:new") == [1001, 1002, 1003]
    assert store.find_card_ids("(tag:spanish OR tag:french) is:new") == [1001]


def test_find_note_and_card_ids_support_unary_not(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)

    assert store.find_note_ids("tag:foo -deck:Lang::French") == [101]
    assert store.find_card_ids("is:review -deck:Default") == [1005]


def test_find_card_and_note_ids_flag_filters(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)

    assert store.find_card_ids("flag:3") == [1002, 1005]
    assert store.find_note_ids("flag:3") == [101, 102]


def test_find_card_and_note_ids_prop_filters(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)

    assert store.find_card_ids("prop:ivl>10") == [1005]
    assert store.find_card_ids("prop:reps>=6") == [1002, 1005]
    assert store.find_note_ids("prop:reps>=6") == [101, 102]
    assert store.find_note_ids("prop:lapses>1") == [102]


def test_find_card_and_note_ids_is_buried(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)

    assert store.find_card_ids("is:buried") == [1008, 1009]
    assert store.find_note_ids("is:buried") == [103, 104]


def test_tag_filter_matches_children_and_tag_none(tmp_path: Path) -> None:
    store = _make_store(
        tmp_path,
        decks=[(1, "Default")],
        notes=[
            (101, " verb ", "a\x1fb", 0),
            (102, " verb::irregular ", "a\x1fb", 0),
            (103, " verbose ", "a\x1fb", 0),
            (104, "", "a\x1fb", 0),
            (105, " adverb::irregular ", "a\x1fb", 0),  # suffix match is not a child of verb
            (106, " ", "a\x1fb", 0),  # hand-edited blank; still untagged
        ],
        cards=[(n + 900, n, 1, 0, 0) for n in (101, 102, 103, 104, 105, 106)],
    )

    assert store.find_note_ids("tag:verb") == [101, 102]
    assert store.find_note_ids("tag:verb::irregular") == [102]
    assert store.find_note_ids("tag:adverb") == [105]
    assert store.find_note_ids("tag:verb*") == [101, 102, 103]
    assert store.find_note_ids("tag:none") == [104, 106]
    assert store.find_card_ids("tag:verb") == [1001, 1002]
    assert store.find_card_ids("-tag:verb") == [1003, 1004, 1005, 1006]
    # rslib: tag:* is every note, tagged or not; a tag with a space matches nothing.
    assert store.find_note_ids("tag:*") == [101, 102, 103, 104, 105, 106]
    assert store.find_note_ids("-tag:*") == []
    assert store.find_note_ids('"tag:a b"') == []


def test_note_prefix_is_an_alias_for_notetype(tmp_path: Path) -> None:
    from anki_cli.db.search_sql import FilterNode, parse

    assert parse("note:Basic") == FilterNode(kind="notetype", value="Basic")
    assert parse("notetype:Basic") == FilterNode(kind="notetype", value="Basic")


def test_added_uses_creation_time_and_the_scheduling_day(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``added:N`` is "created since N rollovers ago", read off the card id
    (creation ms) — not ``notes.mod``, which every edit bumps."""
    now = 1_000_000
    monkeypatch.setattr(direct_mod.time, "time", lambda: now)
    day = 86_400
    # v1 timing (no schedVer): next rollover is the next 86400 boundary.
    next_day_at = (now // day + 1) * day
    last_rollover = next_day_at - day
    ids_ms = {
        "today": (last_rollover + 1) * 1000,
        # Exactly on the cutoff: rslib uses ``c.id > cutoff``, so this is yesterday.
        "on_cutoff": last_rollover * 1000,
        # 23h ago by the wall clock but *before* the last rollover: a wall-clock
        # ``now - 86400`` cutoff would call it added:1; Anki says added:2.
        "late_yesterday": (now - day + 3600) * 1000,
        "yesterday": (next_day_at - 2 * day + 1) * 1000,
        "week_ago": (next_day_at - 8 * day + 1) * 1000,
    }
    assert now - day < ids_ms["late_yesterday"] // 1000 < last_rollover
    store = _make_store(
        tmp_path,
        decks=[(1, "Default")],
        # Edited just now: mod would say "added today" for every note.
        notes=[(n, " t ", "a\x1fb", now) for n in (1, 2, 3, 4, 5)],
        cards=[
            (ids_ms["today"], 1, 1, 0, 0),
            (ids_ms["on_cutoff"], 2, 1, 0, 0),
            (ids_ms["late_yesterday"], 3, 1, 0, 0),
            (ids_ms["yesterday"], 4, 1, 0, 0),
            (ids_ms["week_ago"], 5, 1, 0, 0),
        ],
    )

    assert store.find_card_ids("added:1") == [ids_ms["today"]]
    assert store.find_card_ids("added:2") == sorted(
        [ids_ms["on_cutoff"], ids_ms["late_yesterday"], ids_ms["yesterday"], ids_ms["today"]]
    )
    assert store.find_card_ids("added:30") == sorted(ids_ms.values())
    assert store.find_note_ids("added:1") == [1]
    assert store.find_note_ids("added:2") == [1, 2, 3, 4]
    # rslib parse_added: n.max(1), negatives are a parse error.
    assert store.find_card_ids("added:0") == store.find_card_ids("added:1")
    with pytest.raises(SearchParseError, match="non-negative"):
        store.find_card_ids("added:-1")


def test_unknown_prefix_is_rejected_not_searched_as_text(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)

    for query in ("card:1", "rated:1", "mid:123", "did:1", "dupe:1,x", "front:hola"):
        with pytest.raises(SearchParseError, match="Unsupported filter"):
            store.find_card_ids(query)

    with pytest.raises(SearchParseError, match=r"Supported: added, cid, deck") as excinfo:
        store.find_note_ids("card:1")
    assert excinfo.value.position == 0


def test_escaped_colon_is_a_literal_text_search(tmp_path: Path) -> None:
    store = _make_store(
        tmp_path,
        decks=[(1, "Default")],
        notes=[(101, " t ", "see 12:30\x1fb", 0), (102, " t ", "12 30\x1fb", 0)],
        cards=[(1001, 101, 1, 0, 0), (1002, 102, 1, 0, 0)],
    )

    assert store.find_note_ids(r"12\:30") == [101]
    assert store.find_note_ids(r'"12\:30"') == [101]
    # A leading colon has no prefix and is plain text too.
    assert store.find_note_ids(":30") == [101]
    # Quotes do not protect the colon (Anki: "deck:x" is still a deck search).
    with pytest.raises(SearchParseError, match="Unsupported filter 'x:'"):
        store.find_note_ids('"x:y"')


def test_invalid_queries_raise_parse_errors(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)

    with pytest.raises(SearchParseError, match="Missing closing"):
        store.find_card_ids("(tag:foo OR tag:bar")

    with pytest.raises(SearchParseError, match="Invalid prop filter"):
        store.find_note_ids("prop:ivl>>3")
