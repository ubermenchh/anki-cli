"""Exercise ``_ensure_write_safe`` for real.

Every other write-path test monkeypatches the guard away
(``monkeypatch.setattr(store, "_ensure_write_safe", lambda: None)``), so the
``or`` of the two refusal conditions — and the ``raise`` itself — had no
coverage. These tests keep the guard intact: ``_anki_process_running`` is
stubbed for determinism (a dev box with Anki open must not flake the suite),
while ``_sqlite_write_locked`` runs for real against the tmp collection.

The ``monkeypatch.setattr(detect_mod, ...)`` stubs below work only because
``_ensure_write_safe`` resolves both probes through a call-time
``from anki_cli.backends.detect import ...`` in ``anki_direct.py``. If that
import is ever hoisted to module scope, the stubs must move to
``anki_cli.db.anki_direct._anki_process_running`` / ``._sqlite_write_locked``
instead.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

import anki_cli.backends.detect as detect_mod
from anki_cli.db.anki_direct import AnkiDirectReadStore, DirectWriteBlockedError
from tests.anki_schema import connect
from tests.conftest import Collection, new_collection
from tests.integration.conftest import assert_col_modified, assert_col_untouched


def _make_store(tmp_path: Path) -> tuple[AnkiDirectReadStore, Path]:
    """Real schema; the guard itself is what these tests exercise, so the store
    is *not* made writable here."""
    col = new_collection(tmp_path / "collection.anki2", seed=False)
    col.insert_deck(id=1, name="Default")
    col.insert_notetype(id=10, name="Basic", fields=["Front", "Back"], templates=[])
    return col.store(writable=False), col.db_path


def _insert_note(
    db_path: Path,
    *,
    note_id: int,
    front: str = "F0",
    back: str = "B0",
    tags: str = " old ",
) -> None:
    Collection(db_path).insert_note(
        id=note_id, fields=[front, back], tags=tags, mod=1, usn=-1
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


def test_write_refused_while_anki_process_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``_anki_process_running()`` true must refuse the write on its own.

    ``_sqlite_write_locked`` is left real: the tmp DB is unlocked, so a
    mutated ``and`` falls through and this test goes red.
    """
    store, db_path = _make_store(tmp_path)
    _insert_note(db_path, note_id=1001)
    monkeypatch.setattr(detect_mod, "_anki_process_running", lambda: True)

    with pytest.raises(DirectWriteBlockedError):
        store.update_note(note_id=1001, fields={"Front": "F1"}, tags=None)

    assert _note_row(db_path, 1001)["sfld"] == "F0"
    assert_col_untouched(db_path)


def test_write_refused_while_db_is_write_locked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A second connection holding ``BEGIN IMMEDIATE`` must refuse the write.

    ``_anki_process_running`` is pinned to ``False`` so only the real
    ``_sqlite_write_locked`` probe can trip the guard; a mutated ``and``
    (``False and locked``) falls through and this test goes red.
    """
    store, db_path = _make_store(tmp_path)
    _insert_note(db_path, note_id=1001)
    monkeypatch.setattr(detect_mod, "_anki_process_running", lambda: False)

    locker = sqlite3.connect(str(db_path), isolation_level=None, timeout=1.0)
    locker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(DirectWriteBlockedError):
            store.update_note(note_id=1001, fields={"Front": "F1"}, tags=None)
    finally:
        locker.execute("ROLLBACK")
        locker.close()

    assert _note_row(db_path, 1001)["sfld"] == "F0"
    assert_col_untouched(db_path)


def test_write_refused_when_lock_probe_is_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An unopenable collection must refuse as ``DirectWriteBlockedError``.

    ``_sqlite_write_locked`` re-raises non-lock ``sqlite3.Error`` (fail
    closed); ``_ensure_write_safe`` translates that into the typed refusal —
    an inconclusive probe is "blocked", not a raw ``sqlite3`` traceback out
    of the write path. A directory passes ``exists()`` but
    ``sqlite3.connect`` cannot open it.
    """
    bad = tmp_path / "collection.anki2"
    bad.mkdir()
    store = AnkiDirectReadStore(bad)
    monkeypatch.setattr(detect_mod, "_anki_process_running", lambda: False)

    with pytest.raises(DirectWriteBlockedError, match="Cannot verify the collection lock state"):
        store.update_note(note_id=1, fields={"Front": "F1"}, tags=None)


def test_write_allowed_when_no_anki_process_and_db_unlocked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Pass-through: both probes run for real, then the write lands.

    The spies prove the guard consulted *both* primitives rather than being
    stubbed or short-circuited. The order pin is deliberate: the cheap
    process check runs before probing the live collection with
    ``BEGIN IMMEDIATE``.
    """
    store, db_path = _make_store(tmp_path)
    _insert_note(db_path, note_id=1001)

    consulted: list[str] = []
    real_locked = detect_mod._sqlite_write_locked
    monkeypatch.setattr(
        detect_mod,
        "_anki_process_running",
        lambda: consulted.append("running") or False,
    )
    monkeypatch.setattr(
        detect_mod,
        "_sqlite_write_locked",
        lambda path: consulted.append("locked") or real_locked(path),
    )

    result = store.update_note(note_id=1001, fields={"Front": "F1"}, tags=None)

    assert consulted == ["running", "locked"]
    assert result == {
        "note_id": 1001,
        "updated_fields": True,
        "updated_tags": False,
        "generated_cards": [],
    }
    assert _note_row(db_path, 1001)["sfld"] == "F1"
    assert_col_modified(db_path)


def test_failed_write_rolls_back_and_leaves_note_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A mid-flight error must leave the note byte-identical and col untouched.

    ``_field_checksum`` blows up while the first UPDATE's parameters are being
    built with the transaction already open. The revert mechanism is
    incidental (the except-path ROLLBACK, or ``conn.close()`` discarding the
    open transaction) — the contract this pins is that no partial write
    persists.
    """
    store, db_path = _make_store(tmp_path)
    _insert_note(db_path, note_id=1001)
    monkeypatch.setattr(detect_mod, "_anki_process_running", lambda: False)
    before = _note_row(db_path, 1001)

    def _boom(_first_field: str) -> int:
        raise RuntimeError("checksum boom")

    monkeypatch.setattr(store, "_field_checksum", _boom)

    with pytest.raises(RuntimeError, match="checksum boom"):
        store.update_note(note_id=1001, fields={"Front": "F1"}, tags=None)

    assert _note_row(db_path, 1001) == before
    assert_col_untouched(db_path)


def test_failed_second_write_rolls_back_the_first(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failure *after* a real UPDATE must leave that UPDATE uncommitted too.

    The fields UPDATE executes, then ``_format_tags`` raises while the tags
    UPDATE's parameters are built — a genuine partial write inside the open
    transaction. Whether the except-path ROLLBACK or ``conn.close()`` discards
    it, the contract is that nothing persists and ``col.mod`` stays put.
    """
    store, db_path = _make_store(tmp_path)
    _insert_note(db_path, note_id=1001)
    monkeypatch.setattr(detect_mod, "_anki_process_running", lambda: False)
    before = _note_row(db_path, 1001)

    def _boom(_tags: list[str]) -> str:
        raise RuntimeError("tags boom")

    monkeypatch.setattr(store, "_format_tags", _boom)

    with pytest.raises(RuntimeError, match="tags boom"):
        store.update_note(note_id=1001, fields={"Front": "F1"}, tags=["x"])

    assert _note_row(db_path, 1001) == before
    assert_col_untouched(db_path)
