"""Anki collection schema 18, verbatim, for test fixtures (#37).

Dumped from ``sqlite_master`` of a collection written by Anki Desktop
(``select sql from sqlite_master``), not hand-written: every ``NOT NULL``, the
``COLLATE unicase`` clauses, the ``without rowid`` tables and the indexes are
Anki's own. A fixture built from this cannot omit a column the store reads or
writes, which is how the old per-file builders hid regressions in ``mod`` /
``usn`` / ``mtime_secs`` bookkeeping (a test only sees a column its fixture has).

``unicase`` is Anki's Rust collation; the store registers a Python
implementation on its own connections. Fixture connections must register one
too (``connect()`` below does) or SQLite refuses to create the tables.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 18

# fmt: off
SCHEMA_18_SQL = """
CREATE TABLE cards (
  id integer PRIMARY KEY,
  nid integer NOT NULL,
  did integer NOT NULL,
  ord integer NOT NULL,
  mod integer NOT NULL,
  usn integer NOT NULL,
  type integer NOT NULL,
  queue integer NOT NULL,
  due integer NOT NULL,
  ivl integer NOT NULL,
  factor integer NOT NULL,
  reps integer NOT NULL,
  lapses integer NOT NULL,
  left integer NOT NULL,
  odue integer NOT NULL,
  odid integer NOT NULL,
  flags integer NOT NULL,
  data text NOT NULL
);
CREATE TABLE col (
  id integer PRIMARY KEY,
  crt integer NOT NULL,
  mod integer NOT NULL,
  scm integer NOT NULL,
  ver integer NOT NULL,
  dty integer NOT NULL,
  usn integer NOT NULL,
  ls integer NOT NULL,
  conf text NOT NULL,
  models text NOT NULL,
  decks text NOT NULL,
  dconf text NOT NULL,
  tags text NOT NULL
);
CREATE TABLE config (
  KEY text NOT NULL PRIMARY KEY,
  usn integer NOT NULL,
  mtime_secs integer NOT NULL,
  val blob NOT NULL
) without rowid;
CREATE TABLE deck_config (
  id integer PRIMARY KEY NOT NULL,
  name text NOT NULL COLLATE unicase,
  mtime_secs integer NOT NULL,
  usn integer NOT NULL,
  config blob NOT NULL
);
CREATE TABLE decks (
  id integer PRIMARY KEY NOT NULL,
  name text NOT NULL COLLATE unicase,
  mtime_secs integer NOT NULL,
  usn integer NOT NULL,
  common blob NOT NULL,
  kind blob NOT NULL
);
CREATE TABLE fields (
  ntid integer NOT NULL,
  ord integer NOT NULL,
  name text NOT NULL COLLATE unicase,
  config blob NOT NULL,
  PRIMARY KEY (ntid, ord)
) without rowid;
CREATE TABLE graves (
  oid integer NOT NULL,
  type integer NOT NULL,
  usn integer NOT NULL,
  PRIMARY KEY (oid, type)
) WITHOUT ROWID;
CREATE TABLE notes (
  id integer PRIMARY KEY,
  guid text NOT NULL,
  mid integer NOT NULL,
  mod integer NOT NULL,
  usn integer NOT NULL,
  tags text NOT NULL,
  flds text NOT NULL,
  sfld integer NOT NULL,
  csum integer NOT NULL,
  flags integer NOT NULL,
  data text NOT NULL
);
CREATE TABLE notetypes (
  id integer NOT NULL PRIMARY KEY,
  name text NOT NULL COLLATE unicase,
  mtime_secs integer NOT NULL,
  usn integer NOT NULL,
  config blob NOT NULL
);
CREATE TABLE revlog (
  id integer PRIMARY KEY,
  cid integer NOT NULL,
  usn integer NOT NULL,
  ease integer NOT NULL,
  ivl integer NOT NULL,
  lastIvl integer NOT NULL,
  factor integer NOT NULL,
  time integer NOT NULL,
  type integer NOT NULL
);
CREATE TABLE tags (
  tag text NOT NULL PRIMARY KEY COLLATE unicase,
  usn integer NOT NULL,
  collapsed boolean NOT NULL,
  config blob NULL
) without rowid;
CREATE TABLE templates (
  ntid integer NOT NULL,
  ord integer NOT NULL,
  name text NOT NULL COLLATE unicase,
  mtime_secs integer NOT NULL,
  usn integer NOT NULL,
  config blob NOT NULL,
  PRIMARY KEY (ntid, ord)
) without rowid;
CREATE INDEX idx_cards_odid ON cards (odid) WHERE odid != 0;
CREATE UNIQUE INDEX idx_decks_name ON decks (name);
CREATE UNIQUE INDEX idx_fields_name_ntid ON fields (name, ntid);
CREATE INDEX idx_graves_pending ON graves (usn);
CREATE INDEX idx_notes_mid ON notes (mid);
CREATE UNIQUE INDEX idx_notetypes_name ON notetypes (name);
CREATE INDEX idx_notetypes_usn ON notetypes (usn);
CREATE UNIQUE INDEX idx_templates_name_ntid ON templates (name, ntid);
CREATE INDEX idx_templates_usn ON templates (usn);
CREATE INDEX ix_cards_nid ON cards (nid);
CREATE INDEX ix_cards_sched ON cards (did, queue, due);
CREATE INDEX ix_cards_usn ON cards (usn);
CREATE INDEX ix_notes_csum ON notes (csum);
CREATE INDEX ix_notes_usn ON notes (usn);
CREATE INDEX ix_revlog_cid ON revlog (cid);
CREATE INDEX ix_revlog_usn ON revlog (usn);
"""
# fmt: on

TABLES = (
    "cards", "col", "config", "deck_config", "decks", "fields", "graves",
    "notes", "notetypes", "revlog", "tags", "templates",
)


def _unicase(a: str, b: str) -> int:
    """Case-insensitive ordering; close enough to Anki's for fixtures."""
    la, lb = a.lower(), b.lower()
    return (la > lb) - (la < lb)


def connect(path: str) -> sqlite3.Connection:
    """A connection that can create and read the schema (registers ``unicase``)."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.create_collation("unicase", _unicase)
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_18_SQL)
