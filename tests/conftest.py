"""Repo-wide fixtures (#37).

``collection`` builds a real schema-18 Anki collection in ``tmp_path`` with a
Default deck, a Basic notetype (Front/Back, one template) and a default
``deck_config`` row, and hands back a ``Collection`` whose ``insert_*`` builders
default **every** column to a value the store decodes correctly — protobuf blobs
included — so a test overrides only what it is about.

Why this exists: the direct backend's tests used to build their own partial
schemas (16 variants). A column a fixture omitted was a column no assertion in
that file could see, so a regression in ``mod`` / ``usn`` / ``mtime_secs`` /
``col.mod`` bookkeeping was invisible wherever the fixture happened to lack it.
``assert_synced`` / ``assert_untouched`` make those checks one line.

The protobuf defaults matter:

* ``decks.kind`` — ``bytes(DeckKindContainer())`` is ``b""`` and decodes to kind
  ``"unknown"``, which ``delete_deck`` refuses and ``get_deck_config`` rejects.
  The default here is a *normal* deck on ``config_id=1``.
* ``deck_config.config`` — when a row exists, empty ``learn_steps`` /
  ``relearn_steps`` mean "no steps" (#57), which changes scheduling. The
  default row carries Anki's ``[1, 10]`` / ``[10]``.
* ``notetypes.config`` — ``b""`` decodes fine (kind normal, sort field 0), but
  the default here is explicit so the intent is visible.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

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
    NotetypeConfigKind,
    NotetypeFieldConfig,
    NotetypeTemplateConfig,
)
from tests.anki_schema import SCHEMA_VERSION, connect, create_schema

# Baseline timestamps chosen so tests can assert "moved" without depending on
# the wall clock: negative, so no real or monkeypatched ``time.time()`` (even
# ``lambda: 0``) can collide with them.
COL_BASE_MOD_MS = -1
COL_BASE_SCM_MS = -2

# Row-level baselines for the same reason.
BASE_MOD = 1
BASE_USN = 0

DEFAULT_DECK_ID = 1
DEFAULT_NOTETYPE_ID = 10
DEFAULT_DECK_CONFIG_ID = 1


def normal_deck_kind(*, config_id: int = DEFAULT_DECK_CONFIG_ID, **kw: Any) -> bytes:
    return bytes(DeckKindContainer(normal=DeckNormal(config_id=config_id, **kw)))


def filtered_deck_kind(
    *,
    reschedule: bool = True,
    searches: list[tuple[str, int]] | None = None,
) -> bytes:
    terms = [
        DeckFilteredSearchTerm(search=s, limit=n) for s, n in (searches or [("is:due", 100)])
    ]
    filtered = DeckFiltered(reschedule=reschedule, search_terms=terms)
    return bytes(DeckKindContainer(filtered=filtered))


def deck_config_blob(
    *,
    new_per_day: int = 20,
    reviews_per_day: int = 200,
    desired_retention: float = 0.9,
    maximum_review_interval: int = 36500,
    learn_steps: list[float] | None = None,
    relearn_steps: list[float] | None = None,
    **kw: Any,
) -> bytes:
    return bytes(
        DeckConfigConfig(
            new_per_day=new_per_day,
            reviews_per_day=reviews_per_day,
            desired_retention=desired_retention,
            maximum_review_interval=maximum_review_interval,
            # None -> Anki's defaults; [] is a real "no steps" configuration.
            learn_steps=[1.0, 10.0] if learn_steps is None else learn_steps,
            relearn_steps=[10.0] if relearn_steps is None else relearn_steps,
            **kw,
        )
    )


def notetype_config_blob(
    *, kind: str = "normal", sort_field_idx: int = 0, css: str = "", **kw: Any
) -> bytes:
    kind_enum = (
        NotetypeConfigKind.KIND_CLOZE if kind == "cloze" else NotetypeConfigKind.KIND_NORMAL
    )
    return bytes(NotetypeConfig(kind=kind_enum, sort_field_idx=sort_field_idx, css=css, **kw))


def template_config_blob(front: str, back: str) -> bytes:
    return bytes(NotetypeTemplateConfig(q_format=front, a_format=back))


@dataclass
class Collection:
    """A schema-18 collection file plus typed builders and readers.

    Builders take only what the test cares about; everything else gets a value
    the store decodes correctly. They return the row id. Readers return plain
    dicts. Every method opens its own connection, so the store's connections
    (which hold ``BEGIN IMMEDIATE``) are never contended by the test.
    """

    db_path: Path

    # -- connections -------------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        return connect(str(self.db_path))

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        conn = self.connect()
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def store(self, *, writable: bool = True) -> AnkiDirectReadStore:
        """The store under test. ``writable`` bypasses the "is Anki running"
        guard, which is what every mutator test wants; pass ``False`` to test
        the guard itself."""
        store = AnkiDirectReadStore(self.db_path)
        if writable:
            store._ensure_write_safe = lambda: None  # type: ignore[method-assign]
        return store

    # -- inserts -------------------------------------------------------------------

    def _insert(self, table: str, row: Mapping[str, Any]) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))

    def insert_deck(
        self,
        *,
        id: int,
        name: str,
        kind: bytes | None = None,
        common: bytes | None = None,
        mtime_secs: int = BASE_MOD,
        usn: int = BASE_USN,
    ) -> int:
        self._insert(
            "decks",
            {
                "id": id,
                "name": name,
                "mtime_secs": mtime_secs,
                "usn": usn,
                "common": bytes(DeckCommon()) if common is None else common,
                "kind": normal_deck_kind() if kind is None else kind,
            },
        )
        return id

    def insert_filtered_deck(
        self, *, id: int, name: str, reschedule: bool = True, **kw: Any
    ) -> int:
        return self.insert_deck(
            id=id, name=name, kind=filtered_deck_kind(reschedule=reschedule), **kw
        )

    def insert_deck_config(
        self,
        *,
        id: int = DEFAULT_DECK_CONFIG_ID,
        name: str = "Default",
        config: bytes | None = None,
        mtime_secs: int = BASE_MOD,
        usn: int = BASE_USN,
        **blob_kw: Any,
    ) -> int:
        self._insert(
            "deck_config",
            {
                "id": id,
                "name": name,
                "mtime_secs": mtime_secs,
                "usn": usn,
                "config": deck_config_blob(**blob_kw) if config is None else config,
            },
        )
        return id

    def insert_notetype(
        self,
        *,
        id: int,
        name: str,
        fields: list[str],
        templates: list[tuple[str, str, str]] | None = None,
        kind: str = "normal",
        sort_field_idx: int = 0,
        css: str = "",
        mtime_secs: int = BASE_MOD,
        usn: int = BASE_USN,
    ) -> int:
        """A notetype with its fields and templates (``(name, front, back)``).
        ``templates=None`` means one ``Card 1`` showing ``{{<first field>}}``."""
        self._insert(
            "notetypes",
            {
                "id": id,
                "name": name,
                "mtime_secs": mtime_secs,
                "usn": usn,
                "config": notetype_config_blob(kind=kind, sort_field_idx=sort_field_idx, css=css),
            },
        )
        for ord_, field in enumerate(fields):
            self._insert(
                "fields",
                {"ntid": id, "ord": ord_, "name": field, "config": bytes(NotetypeFieldConfig())},
            )
        if templates is None:
            first = fields[0] if fields else "Front"
            templates = [("Card 1", f"{{{{{first}}}}}", "{{FrontSide}}")]
        for ord_, (tname, front, back) in enumerate(templates):
            self._insert(
                "templates",
                {
                    "ntid": id,
                    "ord": ord_,
                    "name": tname,
                    "mtime_secs": mtime_secs,
                    "usn": usn,
                    "config": template_config_blob(front, back),
                },
            )
        return id

    def insert_note(
        self,
        *,
        id: int,
        fields: list[str],
        mid: int = DEFAULT_NOTETYPE_ID,
        tags: str | list[str] = "",
        mod: int = BASE_MOD,
        usn: int = BASE_USN,
        guid: str | None = None,
        sfld: str | None = None,
        csum: int = 0,
        flags: int = 0,
        data: str = "",
    ) -> int:
        """``tags`` may be a list or Anki's `` a b `` string. ``csum`` defaults
        to 0 on purpose: tests that assert a checksum use the CSUM_* oracle
        literals, never a value derived from the CLI's own hash."""
        tag_text = f" {' '.join(tags)} " if isinstance(tags, list) and tags else (
            tags if isinstance(tags, str) else ""
        )
        self._insert(
            "notes",
            {
                "id": id,
                "guid": f"guid-{id}" if guid is None else guid,
                "mid": mid,
                "mod": mod,
                "usn": usn,
                "tags": tag_text,
                "flds": "\x1f".join(fields),
                "sfld": (fields[0] if fields else "") if sfld is None else sfld,
                "csum": csum,
                "flags": flags,
                "data": data,
            },
        )
        return id

    def insert_card(
        self,
        *,
        id: int,
        nid: int,
        did: int = DEFAULT_DECK_ID,
        ord: int = 0,
        type: int = 0,
        queue: int = 0,
        due: int = 0,
        ivl: int = 0,
        factor: int = 0,
        reps: int = 0,
        lapses: int = 0,
        left: int = 0,
        odue: int = 0,
        odid: int = 0,
        flags: int = 0,
        data: str = "{}",
        mod: int = BASE_MOD,
        usn: int = BASE_USN,
    ) -> int:
        self._insert(
            "cards",
            {
                "id": id, "nid": nid, "did": did, "ord": ord, "mod": mod, "usn": usn,
                "type": type, "queue": queue, "due": due, "ivl": ivl, "factor": factor,
                "reps": reps, "lapses": lapses, "left": left, "odue": odue, "odid": odid,
                "flags": flags, "data": data,
            },
        )
        return id

    def insert_revlog(
        self,
        *,
        id: int,
        cid: int,
        ease: int = 3,
        ivl: int = 1,
        lastIvl: int = 0,
        factor: int = 2500,
        time: int = 0,
        type: int = 1,
        usn: int = BASE_USN,
    ) -> int:
        self._insert(
            "revlog",
            {
                "id": id, "cid": cid, "usn": usn, "ease": ease, "ivl": ivl,
                "lastIvl": lastIvl, "factor": factor, "time": time, "type": type,
            },
        )
        return id

    def set_config(self, key: str, value: Any, *, usn: int = BASE_USN, mtime_secs: int = 0) -> None:
        """A ``config`` row (``schedVer``, ``rollover``, ``creationOffset`` …); the
        store reads ``val`` as JSON."""
        self.execute(
            "INSERT OR REPLACE INTO config (KEY, usn, mtime_secs, val) VALUES (?, ?, ?, ?)",
            (key, usn, mtime_secs, json.dumps(value).encode()),
        )

    # -- reads --------------------------------------------------------------------------

    def row(self, table: str, id: int, *, key: str = "id") -> dict[str, Any]:
        conn = self.connect()
        try:
            found = conn.execute(f"SELECT * FROM {table} WHERE {key} = ?", (id,)).fetchone()
        finally:
            conn.close()
        assert found is not None, f"{table} row {key}={id} not found"
        return dict(found)

    def rows(self, table: str, where: str = "1=1", params: tuple[Any, ...] = (),
             order: str = "rowid") -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            return [dict(r) for r in conn.execute(
                f"SELECT * FROM {table} WHERE {where} ORDER BY {order}", params
            )]
        finally:
            conn.close()

    def ids(self, table: str) -> list[int]:
        return [int(r["id"]) for r in self.rows(table, order="id")]

    def col(self) -> dict[str, Any]:
        return self.rows("col")[0]

    def graves(self) -> list[tuple[int, int, int]]:
        return [(r["oid"], r["type"], r["usn"]) for r in self.rows("graves", order="oid, type")]

    # -- sync bookkeeping ----------------------------------------------------------------

    def assert_synced(self, table: str, id: int, *, key: str = "id") -> dict[str, Any]:
        """The write landed the way Anki's sync needs: the row is flagged for
        upload (``usn = -1``), its timestamp moved off the fixture baseline, and
        ``col.mod`` moved so the next sync scans for ``usn = -1`` rows at all
        (#47). Returns the row for further assertions."""
        row = self.row(table, id, key=key)
        assert row["usn"] == -1, f"{table} {id}: usn={row['usn']}, expected -1"
        stamp = "mtime_secs" if "mtime_secs" in row else "mod"
        assert row[stamp] > BASE_MOD, f"{table} {id}: {stamp}={row[stamp]} did not move"
        assert self.col()["mod"] > COL_BASE_MOD_MS, "col.mod did not move"
        return row

    def assert_schema_changed(self) -> None:
        col = self.col()
        assert col["mod"] > COL_BASE_MOD_MS
        assert col["scm"] > COL_BASE_SCM_MS, "col.scm did not move (full sync not requested)"

    def assert_untouched(self) -> None:
        """A refused write must leave no trace: ``col.mod`` still at baseline."""
        col = self.col()
        assert col["mod"] == COL_BASE_MOD_MS, f"col.mod moved to {col['mod']}"
        assert col["scm"] == COL_BASE_SCM_MS, f"col.scm moved to {col['scm']}"


def new_collection(path: Path, *, crt: int = 0, seed: bool = True) -> Collection:
    """Create the file. ``seed=False`` gives bare tables plus the ``col`` row."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(str(path))
    try:
        create_schema(conn)
        conn.execute(
            "INSERT INTO col (id, crt, mod, scm, ver, dty, usn, ls, conf, models, decks, dconf,"
            " tags) VALUES (1, ?, ?, ?, ?, 0, 0, 0, '{}', '{}', '{}', '{}', '{}')",
            (crt, COL_BASE_MOD_MS, COL_BASE_SCM_MS, SCHEMA_VERSION),
        )
        conn.commit()
    finally:
        conn.close()
    col = Collection(path)
    if seed:
        col.insert_deck_config()
        col.insert_deck(id=DEFAULT_DECK_ID, name="Default")
        col.insert_notetype(
            id=DEFAULT_NOTETYPE_ID,
            name="Basic",
            fields=["Front", "Back"],
            templates=[("Card 1", "{{Front}}", "{{FrontSide}}<hr id=answer>{{Back}}")],
        )
    return col


@pytest.fixture
def collection(tmp_path: Path) -> Iterator[Collection]:
    """Seeded schema-18 collection (Default deck, Basic notetype, deck_config 1)."""
    yield new_collection(tmp_path / "collection.anki2")


@pytest.fixture
def bare_collection(tmp_path: Path) -> Iterator[Collection]:
    """Schema only plus the ``col`` row; tests seed what they need."""
    yield new_collection(tmp_path / "collection.anki2", seed=False)
