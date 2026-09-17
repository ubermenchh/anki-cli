from __future__ import annotations

import json
import math
import random
import re
import sqlite3
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha1
from html.entities import name2codepoint
from pathlib import Path
from typing import Any, cast

import betterproto
from fsrs import Card as FSRSCard
from fsrs import Rating, ReviewLog, Scheduler, State
from fsrs.scheduler import LOWER_BOUNDS_PARAMETERS, UPPER_BOUNDS_PARAMETERS

from anki_cli.core.search import (
    SearchContext,
    compile_card_query,
    compile_note_query,
    escape_like,
)
from anki_cli.core.template import (
    _CLOZE_RE,
    SPECIAL_FIELDS,
    TemplateParseError,
    field_is_empty,
    parse_template,
    remove_field_from_template,
    template_renders_with_fields,
    template_requirements,
)
from anki_cli.db.timing import (
    DEFAULT_ROLLOVER_HOUR,
    SchedTiming,
    local_minutes_west_for_stamp,
    sched_timing_today,
)
from anki_cli.models.output import JSONValue
from anki_cli.proto.anki.deck_config import DeckConfigConfig
from anki_cli.proto.anki.decks import DeckCommon, DeckKindContainer
from anki_cli.proto.anki.notetypes import (
    NotetypeConfig,
    NotetypeConfigCardRequirement,
    NotetypeConfigCardRequirementKind,
    NotetypeConfigKind,
    NotetypeFieldConfig,
    NotetypeTemplateConfig,
)

# Anki stores two different units in cards.due for learning cards: intraday
# learning (queue 1) holds a unix epoch in seconds, day-learn (queue 3) holds a
# day index relative to col.crt. rslib tells them apart with this threshold
# (Card::restore_queue_from_type); any epoch after 2001-09-09 exceeds it and
# no plausible day index ever will.
LEARN_DUE_EPOCH_THRESHOLD = 1_000_000_000


def is_intraday_learn_due(due: int) -> bool:
    return due > LEARN_DUE_EPOCH_THRESHOLD


# A card parked in a filtered deck keeps its real due in ``odue`` while ``due``
# holds a position; rslib's restore_queue_after_bury_or_suspend reads
# ``original_due`` when it is set. Use this wherever the *scheduling* due of a
# card is needed regardless of where it currently lives.
RESTORED_DUE_SQL = "CASE WHEN odue > 0 THEN odue ELSE due END"

# Same, but only for a card that is actually on loan (rslib
# remove_from_filtered_deck_restoring_queue returns early when odid == 0, so a
# stray odue on a home-deck card must not rewrite its due).
RESTORED_DUE_IF_ON_LOAN_SQL = "CASE WHEN odid != 0 AND odue > 0 THEN odue ELSE due END"

# rslib Card::remove_from_filtered_deck_before_reschedule: a card that is about
# to be given a brand-new schedule (answer, forget, set due date) first goes
# home; the caller then writes queue/due itself.
LEAVE_FILTERED_DECK_SQL = "did = CASE WHEN odid != 0 THEN odid ELSE did END, odid = 0, odue = 0"


def queue_from_type_sql(*, due_expr: str = RESTORED_DUE_SQL) -> str:
    """SQL CASE recomputing ``queue`` from ``type`` (rslib ``restore_queue_from_type``).

    Used when a card leaves the suspended/buried/filtered state. ``due_expr`` is
    the expression holding the card's scheduling due *after* the surrounding
    UPDATE, which matters because SQLite evaluates SET clauses against the
    pre-update row. The default reads ``odue`` for cards in a filtered deck.
    """
    return f"""CASE
                        WHEN type = 0 THEN 0
                        WHEN type IN (1, 3) THEN
                            CASE WHEN ({due_expr}) > {LEARN_DUE_EPOCH_THRESHOLD} THEN 1 ELSE 3 END
                        ELSE 2
                    END"""


# rslib ``HTML_MEDIA_TAGS``: ``<img src="x.png">`` and friends contribute their
# filename to the checksum text; every other tag is dropped afterwards.
# Output is byte-identical to the rslib regex on its test vectors; the one
# accepted divergence is any quote character that is not part of a balanced
# pair inside a media tag — e.g. the unquoted ``<img alt=Bob's src="a.png">``
# (rslib keeps "a.png", this drops the tag), likewise the unbalanced
# ``<img alt="a src=x.png>``. The quoted form ``alt="Bob's"`` is fine, and
# Anki's editor always quotes, so this needs hand-written HTML.
_CHECKSUM_MEDIA_TAG_RE = re.compile(
    r"""
    <\b(?:img|audio|video|object|source)\b
    (?:[^>"']|"[^"]*"|'[^']*')+?  # disjoint alternatives: a quote char is only
                                # consumable by the quoted branch, so a src-less
                                # tag cannot enumerate 2**n splits of its attrs
    \b(?:src|data)\b=
    (?:
        "([^"]+?)"[^>]*>
        |'([^']+?)'[^>]*>
        |([^ >]+?)(?:\x20[^>]*>|>)
    )
    """,
    re.VERBOSE | re.IGNORECASE | re.DOTALL,
)

# rslib ``HTML``: comments, style/script bodies, then any remaining tag.
# ``<style[^>]*>`` is match-equivalent to ``<style.*?>`` (the first '>' ends the
# tag either way) but single-parse — ``.*?`` was cubic on repeated unclosed
# ``<style>``/``<script>`` tags.
_CHECKSUM_HTML_TAG_RE = re.compile(
    r"(<!--.*?-->)|(<style[^>]*>.*?</style>)|(<script[^>]*>.*?</script>)|(<.*?>)",
    re.IGNORECASE | re.DOTALL,
)

# rslib ``decode_entities``: htmlescape::decode_html is all-or-nothing — any
# malformed entity (bare '&', unknown name, bad numeric escape) errs and the
# ORIGINAL text is kept, so Python's lenient ``html.unescape`` cannot be used:
# it decodes what it can and also accepts HTML5 legacy no-semicolon forms
# (``&nbsp``/``&amp``/``&copy``) that htmlescape rejects.
_ENTITY_RE = re.compile(r"&(#[0-9]+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);")


def _decode_entities_strict(text: str) -> str:
    """Decode entities with htmlescape semantics: all-or-nothing.

    Faithfulness notes, verified against htmlescape's ``decode.rs``:

    - ``&#x`` is lowercase-only upstream (``&#X41;`` → MalformedNumEscape).
    - Numeric escapes resolving to UTF-16 surrogates (``&#xD800;``) err via
      ``char::from_u32`` → InvalidCharacter; Python's ``chr`` would accept them
      and then crash the UTF-8 encode downstream.
    - Named entities use the HTML4 table (``name2codepoint``), which matches
      htmlescape's ``NAMED_ENTITIES`` exactly (252 names, same codepoints);
      HTML5-only names like ``&check;`` err → UnknownEntity.
    """
    if "&" not in text:
        return text
    # A '&' not part of a well-formed entity errs → keep the text as-is.
    if "&" in _ENTITY_RE.sub("", text):
        return text

    def _one(match: re.Match[str]) -> str:
        body = match.group(1)
        if body[0] == "#":
            cp = int(body[2:], 16) if body[1] == "x" else int(body[1:])
            if 0xD800 <= cp <= 0xDFFF:
                raise ValueError("surrogate code point")
            return chr(cp)
        return chr(name2codepoint[body])

    try:
        return _ENTITY_RE.sub(_one, text).replace("\xa0", " ")
    except (KeyError, ValueError, OverflowError):
        return text


def _strip_html_preserving_media_filenames(text: str) -> str:
    """Port of rslib ``strip_html_preserving_media_filenames``.

    Media tags (``img``/``audio``/``video``/``object``/``source``) are replaced
    by their ``src``/``data`` filename surrounded by spaces, remaining markup is
    removed, and entities are decoded. ``<b>Q</b>`` therefore checksums
    identically to ``Q``.
    """

    def _media_filename(match: re.Match[str]) -> str:
        return " " + "".join(group or "" for group in match.groups()) + " "

    # Mirrors rslib's borrowed-Cow fast path: no markup → nothing to strip.
    stripped = text
    if "<" in stripped:
        stripped = _CHECKSUM_MEDIA_TAG_RE.sub(_media_filename, stripped)
        stripped = _CHECKSUM_HTML_TAG_RE.sub("", stripped)
    if "&" in stripped:
        stripped = _decode_entities_strict(stripped)
    return stripped


class DirectWriteBlockedError(RuntimeError):
    """Direct write refused: Anki Desktop is running or holds the collection lock.

    Raised by ``_ensure_write_safe`` so callers can catch the refusal by type
    instead of matching the message text.
    """


# py-fsrs 6 wants exactly 21 weights. Anki may still carry FSRS-4.5 (17) or
# FSRS-5 (19) weights from an older optimizer run; fsrs-rs upgrades those with
# fixed transforms (model_v6::check_and_fill_parameters_fsrs6), mirrored here.
FSRS6_PARAM_COUNT = 21
FSRS5_DEFAULT_DECAY = 0.5
FSRS6_DEFAULT_PARAMETERS: tuple[float, ...] = (
    0.212, 1.2931, 2.3065, 8.2956, 6.4133, 0.8334, 3.0194, 0.001, 1.8722, 0.1666, 0.796,
    1.4835, 0.0614, 0.2629, 1.6483, 0.6014, 1.8729, 0.5425, 0.0912, 0.0658, 0.1542,
)

# Anki's revlog.type (RevlogReviewKind).
REVLOG_KIND_LEARNING = 0
REVLOG_KIND_REVIEW = 1
REVLOG_KIND_RELEARNING = 2
REVLOG_KIND_FILTERED = 3  # also used for a review answered before it was due


def upgrade_fsrs_parameters(values: list[float]) -> tuple[list[float], str]:
    """Return 21 FSRS-6 weights plus a label describing where they came from.

    Port of fsrs-rs ``check_and_fill_parameters_fsrs6``: 17 (FSRS-4.5) and 19
    (FSRS-5) weight sets are transformed the way Anki transforms them before
    scheduling; anything else falls back to the FSRS-6 defaults.
    """
    n = len(values)
    if n == FSRS6_PARAM_COUNT:
        return list(values), "fsrs6"
    if n == 19:
        return [*values, 0.0, FSRS5_DEFAULT_DECAY], "fsrs5-upgraded"
    if n == 17:
        w = list(values)
        if w[5] * 3.0 + 1.0 <= 0.0:
            # Corrupt blob; the log below would raise. Anki-optimized w5 >= 0.1.
            return list(FSRS6_DEFAULT_PARAMETERS), "default"
        w[4] = w[5] * 2.0 + w[4]
        w[5] = math.log(w[5] * 3.0 + 1.0) / 3.0
        w[6] += 0.5
        return [*w, 0.0, 0.0, 0.0, FSRS5_DEFAULT_DECAY], "fsrs4.5-upgraded"
    return list(FSRS6_DEFAULT_PARAMETERS), "default"


def clamp_fsrs_parameters(values: list[float]) -> tuple[list[float], bool]:
    """Clamp 21 weights into py-fsrs's accepted range (fsrs-rs ``parameter_clipper``).

    Anki schedules with upgraded legacy weights as-is; py-fsrs refuses anything
    out of bounds, so clamping keeps the user's optimized weights instead of
    throwing the whole set away. Returns the clamped list and whether anything
    changed.
    """
    clamped = [
        min(max(float(w), float(lo)), float(hi))
        for w, lo, hi in zip(values, LOWER_BOUNDS_PARAMETERS, UPPER_BOUNDS_PARAMETERS, strict=True)
    ]
    return clamped, clamped != [float(w) for w in values]


def fsrs_fuzz_seed(card_id: int, reps: int) -> int:
    """rslib ``get_fuzz_seed_for_id_and_reps``: the same card at the same rep
    count always fuzzes the same way, so a preview matches the later answer."""
    return (int(card_id) + int(reps)) & 0xFFFFFFFFFFFFFFFF


# Lowest collection schema this module understands: separate decks / notetypes /
# fields / templates / deck_config tables holding protobuf blobs. Anki upgrades a
# profile to it on first open with 2.1.50+ (2022); older files (schema 11) keep
# everything as JSON inside the col table and have none of those tables.
MIN_SUPPORTED_SCHEMA_VERSION = 18


class UnsupportedCollectionError(RuntimeError):
    """The collection file is real but its schema is older than we can read or write."""


class NoteRejectedError(ValueError):
    """``add_note`` refused the note before writing anything.

    Base for the per-note refusals that mirror AnkiConnect's ``addNote`` checks
    (rslib ``note_fields_check``); ``add_notes`` treats these as per-item failures
    and reports ``None`` for the item, while every other error propagates.
    """


class DuplicateNoteError(NoteRejectedError):
    """A note of the same notetype already has this first field.

    Mirrors AnkiConnect's "cannot create note because it is a duplicate"; lifted by
    ``allow_duplicate``. The message states the fact only — the CLI layer appends
    the ``--allow-duplicate`` remedy, since this module has no CLI surface.
    """

    # ``duplicate_ids`` always carries the full list; the message stays one readable
    # line even after a large ``--allow-duplicate`` import.
    MAX_IDS_IN_MESSAGE = 10

    def __init__(self, *, notetype: str, duplicate_ids: list[int]) -> None:
        self.notetype = notetype
        self.duplicate_ids = duplicate_ids
        shown = duplicate_ids[: self.MAX_IDS_IN_MESSAGE]
        ids = ", ".join(str(i) for i in shown)
        if len(duplicate_ids) > len(shown):
            ids += f" and {len(duplicate_ids) - len(shown)} more"
        super().__init__(
            f"Duplicate note: first field matches existing note(s) {ids} "
            f"in notetype '{notetype}'."
        )


class EmptyNoteError(NoteRejectedError):
    """The first field is empty once markup is ignored.

    Mirrors AnkiConnect's "cannot create note because it is empty" (rslib
    ``NoteFieldsState::Empty``). Not lifted by ``allow_duplicate``.
    """

    def __init__(self, *, notetype: str, field_name: str) -> None:
        self.notetype = notetype
        self.field_name = field_name
        super().__init__(
            f"Empty note: first field '{field_name}' of notetype '{notetype}' is empty."
        )


class AnkiDirectReadStore:
    """Helpers for Anki's collection(.anki21b/.anki2) schema."""

    def __init__(self, db_path: Path) -> None:
        resolved = db_path.expanduser().resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"Direct DB not found: {resolved}")
        self.db_path = resolved
        self._check_schema_version()

    def _check_schema_version(self) -> None:
        """Refuse legacy collections up front instead of failing mid-command.

        Only the schema version is inspected; a file without a readable
        ``col.ver`` (a non-Anki SQLite file, or a stripped-down test fixture)
        is left for later queries to reject on their own terms.
        """
        # Plain path + query_only rather than a file: URI: SQLite's URI parser
        # treats '#' and '?' in the path as delimiters, so a profile named
        # "Deck#1" would silently open (and create) a different file. The file
        # is known to exist, so the default rwc open cannot create anything.
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=1.0)
        except sqlite3.Error:
            return
        try:
            conn.execute("PRAGMA query_only = ON")
            row = conn.execute("SELECT ver FROM col LIMIT 1").fetchone()
        except sqlite3.Error:
            return
        finally:
            conn.close()
        if row is None or row[0] is None:
            return
        try:
            ver = int(row[0])
        except (TypeError, ValueError):
            return
        if ver < MIN_SUPPORTED_SCHEMA_VERSION:
            raise UnsupportedCollectionError(
                f"Unsupported collection schema {ver} at {self.db_path} "
                f"(need >= {MIN_SUPPORTED_SCHEMA_VERSION}). Open the profile once in "
                "Anki 2.1.50 or newer to upgrade it, then retry."
            )

    @staticmethod
    def _unicase_collation(left: str | None, right: str | None) -> int:
        lval = (left or "").casefold()
        rval = (right or "").casefold()
        return (lval > rval) - (lval < rval)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=1.0)
        conn.row_factory = sqlite3.Row
        conn.create_collation("unicase", self._unicase_collation)
        conn.execute("PRAGMA query_only = ON")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _connect_write(self) -> Iterator[sqlite3.Connection]:
        self._ensure_write_safe()
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.create_collation("unicase", self._unicase_collation)
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            conn.execute("BEGIN IMMEDIATE")
            changes_before = conn.total_changes
            yield conn
            if conn.total_changes != changes_before:
                # Anki's sync handshake compares col.mod with the server; rows
                # marked usn = -1 are only scanned if that timestamp moved, so a
                # write that leaves col.mod alone is invisible to the next sync.
                self._touch_collection_modified(conn)
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def _touch_collection_modified(self, conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE col SET mod = ?", (self._now_ms(),))

    def _mark_schema_modified(self, conn: sqlite3.Connection) -> None:
        """Record a schema change (Anki: ``set_schema_modified``).

        A differing ``col.scm`` forces a one-way full sync on the next sync, which
        Anki requires whenever fields or templates are added, removed or
        reordered, or the sort field changes.
        """
        now_ms = self._now_ms()
        conn.execute("UPDATE col SET scm = ?, mod = ?", (now_ms, now_ms))

    # ---- deck / notetype -------------------------------------------------

    def get_decks(self) -> list[dict[str, JSONValue]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, name, common, kind
                FROM decks
                ORDER BY LOWER(name), id
                """
            ).fetchall()
            deck_config_map = self._read_deck_config_map(conn)

        output: list[dict[str, JSONValue]] = []
        for row in rows:
            did = int(row["id"])
            name = str(row["name"])
            common = self._decode_deck_common(bytes(row["common"] or b""), did=did)
            kind = self._decode_deck_kind(bytes(row["kind"] or b""), did=did)

            kind_name, kind_msg = betterproto.which_one_of(kind, "kind")
            item: dict[str, JSONValue] = {
                "id": did,
                "name": name,
                "kind": kind_name or "unknown",
                "stats": {
                    "new_studied": int(common.new_studied),
                    "review_studied": int(common.review_studied),
                    "learning_studied": int(common.learning_studied),
                },
            }

            if kind_name == "normal" and kind_msg is not None:
                config_id = int(kind_msg.config_id)
                item["config_id"] = config_id
                item["description"] = str(kind_msg.description or "")
                item["new_limit"] = (
                    int(kind_msg.new_limit) if kind_msg.new_limit is not None else None
                )
                item["review_limit"] = (
                    int(kind_msg.review_limit) if kind_msg.review_limit is not None else None
                )
                if config_id in deck_config_map:
                    item["config"] = deck_config_map[config_id]

            elif kind_name == "filtered" and kind_msg is not None:
                item["search_terms"] = [term.search for term in kind_msg.search_terms]
                item["reschedule"] = bool(kind_msg.reschedule)

            output.append(item)

        return output

    def get_deck(self, name: str) -> dict[str, JSONValue]:
        normalized = name.strip()
        if not normalized:
            raise ValueError("Deck name cannot be empty.")

        deck = next(
            (item for item in self.get_decks() if str(item.get("name", "")) == normalized),
            None,
        )
        if deck is None:
            raise LookupError(f"Deck not found: {normalized}")

        return {
            **deck,
            "due_counts": self.get_due_counts(deck=normalized),
            "next_due": self._get_next_due_for_deck(normalized),
        }

    def get_deck_config(self, name: str) -> dict[str, JSONValue]:
        normalized = name.strip()
        if not normalized:
            raise ValueError("Deck name cannot be empty.")

        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, kind FROM decks WHERE name = ?",
                (normalized,),
            ).fetchone()
            if row is None:
                raise LookupError(f"Deck not found: {normalized}")

            did = int(row["id"])
            deck_kind = self._decode_deck_kind(bytes(row["kind"] or b""), did=did)
            kind_name, kind_msg = betterproto.which_one_of(deck_kind, "kind")
            if kind_name != "normal" or kind_msg is None:
                raise ValueError(f"Deck '{normalized}' is not a normal deck.")

            config_id = int(kind_msg.config_id)
            cfg_row = conn.execute(
                "SELECT id, name, config FROM deck_config WHERE id = ?",
                (config_id,),
            ).fetchone()
            if cfg_row is None:
                raise LookupError(f"Deck config not found: {config_id}")

        cfg = self._decode_deck_config(bytes(cfg_row["config"] or b""), dcid=config_id)
        return {
            "deck": normalized,
            "config_id": config_id,
            "config_name": str(cfg_row["name"]),
            "config": self._deck_config_to_dict(cfg),
        }

    def get_notetypes(self) -> list[dict[str, JSONValue]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, name
                FROM notetypes
                ORDER BY LOWER(name), id
                """
            ).fetchall()
            fields_by_ntid, templates_by_ntid = self._load_notetype_parts(conn)

        result: list[dict[str, JSONValue]] = []
        for row in rows:
            ntid = int(row["id"])
            fields = fields_by_ntid.get(ntid, [])
            templates = templates_by_ntid.get(ntid, [])

            result.append(
                {
                    "id": ntid,
                    "name": str(row["name"]),
                    "field_count": len(fields),
                    "template_count": len(templates),
                    "fields": [str(item["name"]) for item in fields],
                    "templates": [str(item["name"]) for item in templates],
                }
            )
        return result

    def get_notetype(self, name: str) -> dict[str, JSONValue]:
        normalized = name.strip()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, name, config
                FROM notetypes
                WHERE name = ?
                """,
                (normalized,),
            ).fetchone()

            if row is None:
                raise LookupError(f"Notetype not found: {normalized}")

            ntid = int(row["id"])
            fields_by_ntid, templates_by_ntid = self._load_notetype_parts(conn)

        config = self._decode_notetype_config(bytes(row["config"] or b""), ntid=ntid)
        fields = fields_by_ntid.get(ntid, [])
        templates = templates_by_ntid.get(ntid, [])

        templates_map: dict[str, JSONValue] = {
            str(item["name"]): {
                "Front": str(item["qfmt"]),
                "Back": str(item["afmt"]),
                "ord": self._coerce_int_value(item.get("ord")) or 0,
            }
            for item in templates
        }

        kind = "cloze" if int(config.kind) == 1 else "normal"

        return {
            "id": ntid,
            "name": str(row["name"]),
            "kind": kind,
            "sort_field_idx": int(config.sort_field_idx),
            "fields": [str(item["name"]) for item in fields],
            "templates": templates_map,
            "styling": {"css": config.css},
            "requirements": [
                {
                    "card_ord": int(req.card_ord),
                    "kind": int(req.kind),
                    "field_ords": [int(x) for x in req.field_ords],
                }
                for req in config.reqs
            ],
        }

    def create_notetype(
        self,
        *,
        name: str,
        fields: list[str],
        templates: list[dict[str, str]],
        css: str = "",
        kind: str = "normal",
    ) -> dict[str, JSONValue]:
        normalized = name.strip()
        if not normalized:
            raise ValueError("Notetype name cannot be empty.")
        field_names = [field.strip() for field in fields if field.strip()]
        if not field_names:
            raise ValueError("At least one field is required.")

        cleaned_templates: list[dict[str, str]] = []
        for item in templates:
            tname = str(item.get("name", "")).strip()
            if not tname:
                raise ValueError("Template name cannot be empty.")
            cleaned_templates.append(
                {
                    "name": tname,
                    "front": str(item.get("front", "")),
                    "back": str(item.get("back", "")),
                }
            )
        if not cleaned_templates:
            raise ValueError("At least one template is required.")

        normalized_kind = kind.strip().lower()
        if normalized_kind not in {"normal", "cloze"}:
            raise ValueError("kind must be 'normal' or 'cloze'.")

        with self._connect_write() as conn:
            existing = conn.execute(
                "SELECT id FROM notetypes WHERE name = ?",
                (normalized,),
            ).fetchone()
            if existing is not None:
                raise ValueError(f"Notetype already exists: {normalized}")

            ntid = self._allocate_row_id(conn, "notetypes")
            now_sec = int(time.time())
            config = NotetypeConfig(
                kind=(
                    NotetypeConfigKind.KIND_CLOZE
                    if normalized_kind == "cloze"
                    else NotetypeConfigKind.KIND_NORMAL
                ),
                sort_field_idx=0,
                css=css,
            )
            conn.execute(
                """
                INSERT INTO notetypes (id, name, mtime_secs, usn, config)
                VALUES (?, ?, ?, -1, ?)
                """,
                (ntid, normalized, now_sec, bytes(config)),
            )

            for ord_, field_name in enumerate(field_names):
                conn.execute(
                    """
                    INSERT INTO fields (ntid, ord, name, config)
                    VALUES (?, ?, ?, ?)
                    """,
                    (ntid, ord_, field_name, bytes(NotetypeFieldConfig())),
                )

            for ord_, template in enumerate(cleaned_templates):
                conn.execute(
                    """
                    INSERT INTO templates (ntid, ord, name, mtime_secs, usn, config)
                    VALUES (?, ?, ?, ?, -1, ?)
                    """,
                    (
                        ntid,
                        ord_,
                        template["name"],
                        now_sec,
                        bytes(
                            NotetypeTemplateConfig(
                                q_format=template["front"],
                                a_format=template["back"],
                            )
                        ),
                    ),
                )
            self._recompute_reqs(conn, ntid)

        return {
            "id": ntid,
            "name": normalized,
            "kind": normalized_kind,
            "field_count": len(field_names),
            "template_count": len(cleaned_templates),
            "created": True,
        }

    def add_notetype_field(self, *, name: str, field_name: str) -> dict[str, JSONValue]:
        normalized_name = name.strip()
        normalized_field = field_name.strip()
        if not normalized_name or not normalized_field:
            raise ValueError("Notetype and field name are required.")

        with self._connect_write() as conn:
            row = conn.execute(
                "SELECT id FROM notetypes WHERE name = ?",
                (normalized_name,),
            ).fetchone()
            if row is None:
                raise LookupError(f"Notetype not found: {normalized_name}")
            ntid = int(row["id"])

            existing = conn.execute(
                "SELECT 1 FROM fields WHERE ntid = ? AND name = ?",
                (ntid, normalized_field),
            ).fetchone()
            if existing is not None:
                return {"name": normalized_name, "field": normalized_field, "added": False}

            max_ord_row = conn.execute(
                "SELECT COALESCE(MAX(ord), -1) AS max_ord FROM fields WHERE ntid = ?",
                (ntid,),
            ).fetchone()
            next_ord = int(max_ord_row["max_ord"]) + 1
            field_count_before = int(
                conn.execute(
                    "SELECT COUNT(*) FROM fields WHERE ntid = ?", (ntid,)
                ).fetchone()[0]
            )
            now_sec = int(time.time())
            conn.execute(
                """
                INSERT INTO fields (ntid, ord, name, config)
                VALUES (?, ?, ?, ?)
                """,
                (ntid, next_ord, normalized_field, bytes(NotetypeFieldConfig())),
            )
            # notes.flds is positional: every existing note needs an empty slot
            # appended or Anki reports a field-count mismatch.
            updated_notes = self._append_field_to_notes(
                conn, ntid=ntid, field_count=field_count_before, now_sec=now_sec
            )
            conn.execute(
                "UPDATE notetypes SET mtime_secs = ?, usn = -1 WHERE id = ?",
                (now_sec, ntid),
            )
            self._recompute_reqs(conn, ntid)
            self._mark_schema_modified(conn)

        return {
            "name": normalized_name,
            "field": normalized_field,
            "added": True,
            "updated_notes": updated_notes,
            "full_sync_required": True,
        }

    def remove_notetype_field(self, *, name: str, field_name: str) -> dict[str, JSONValue]:
        normalized_name = name.strip()
        normalized_field = field_name.strip()
        if not normalized_name or not normalized_field:
            raise ValueError("Notetype and field name are required.")

        with self._connect_write() as conn:
            row = conn.execute(
                "SELECT id, config FROM notetypes WHERE name = ?",
                (normalized_name,),
            ).fetchone()
            if row is None:
                raise LookupError(f"Notetype not found: {normalized_name}")

            ntid = int(row["id"])
            fields = conn.execute(
                "SELECT ord, name FROM fields WHERE ntid = ? ORDER BY ord",
                (ntid,),
            ).fetchall()
            if len(fields) <= 1:
                raise ValueError("Cannot remove the last remaining field.")

            # Anki declares fields.name COLLATE unicase, so match case-insensitively
            # (add_notetype_field's `name = ?` lookup already does via SQL).
            wanted = normalized_field.casefold()
            target_row = next(
                (item for item in fields if str(item["name"]).casefold() == wanted),
                None,
            )
            if target_row is None:
                raise LookupError(f"Field not found: {normalized_field}")
            removed_ord = int(target_row["ord"])
            stored_field_name = str(target_row["name"])
            old_field_count = len(fields)
            new_field_count = old_field_count - 1
            now_sec = int(time.time())

            conn.execute(
                "DELETE FROM fields WHERE ntid = ? AND ord = ?",
                (ntid, removed_ord),
            )
            conn.execute(
                "UPDATE fields SET ord = ord - 1 WHERE ntid = ? AND ord > ?",
                (ntid, removed_ord),
            )

            # Keep the notetype config consistent with the new field layout.
            config = self._decode_notetype_config(bytes(row["config"] or b""), ntid=ntid)
            sort_idx = int(config.sort_field_idx)
            if sort_idx > removed_ord:
                sort_idx -= 1
            # If the sort field itself was removed, Anki (reposition_sort_idx) keeps
            # the ordinal, so the field that slides into that slot becomes the sort
            # field; the clamp only matters when the removed field was the last one.
            sort_idx = max(0, min(sort_idx, new_field_count - 1))
            config.sort_field_idx = sort_idx

            conn.execute(
                "UPDATE notetypes SET mtime_secs = ?, usn = -1, config = ? WHERE id = ?",
                (now_sec, bytes(config), ntid),
            )
            # Anki drops references to the removed field from every template
            # (falling back to the first remaining field if a front would be
            # left empty), then recomputes the legacy reqs cache from the result.
            remaining_names = [str(f["name"]) for f in fields if int(f["ord"]) != removed_ord]
            self._remove_field_from_templates(
                conn,
                ntid=ntid,
                removed_name=stored_field_name,
                first_remaining_field=remaining_names[0],
                is_cloze=int(config.kind) == 1,
                now_sec=now_sec,
            )
            self._recompute_reqs(conn, ntid)
            self._mark_schema_modified(conn)

            # Field values are stored positionally in notes.flds, so every note of
            # this notetype must drop the removed slot or all later fields shift.
            updated_notes = self._remove_field_from_notes(
                conn,
                ntid=ntid,
                removed_ord=removed_ord,
                field_count=old_field_count,
                sort_idx=sort_idx,
                now_sec=now_sec,
            )

        return {
            "name": normalized_name,
            "field": stored_field_name,
            "removed": True,
            "updated_notes": updated_notes,
            "full_sync_required": True,
        }

    def _append_field_to_notes(
        self,
        conn: sqlite3.Connection,
        *,
        ntid: int,
        field_count: int,
        now_sec: int,
    ) -> int:
        """Append one empty slot to every note's positional field list.

        ``field_count`` is the number of fields *before* the addition; short
        (legacy) rows are padded to it and over-long rows truncated to it first
        so the new slot lands at the right ordinal. Mirrors Anki's
        ``Note::reorder_fields`` for the append case.
        """
        note_rows = conn.execute(
            "SELECT id, flds FROM notes WHERE mid = ?",
            (ntid,),
        ).fetchall()
        if not note_rows:
            return 0

        updates: list[tuple[str, int, int]] = []
        for note_row in note_rows:
            values = self._split_fields(str(note_row["flds"] or ""))[:field_count]
            if len(values) < field_count:
                values.extend([""] * (field_count - len(values)))
            values.append("")
            updates.append(("\x1f".join(values), now_sec, int(note_row["id"])))

        conn.executemany(
            "UPDATE notes SET flds = ?, mod = ?, usn = -1 WHERE id = ?",
            updates,
        )
        return len(updates)

    def _remove_field_from_notes(
        self,
        conn: sqlite3.Connection,
        *,
        ntid: int,
        removed_ord: int,
        field_count: int,
        sort_idx: int,
        now_sec: int,
    ) -> int:
        """Drop ``removed_ord`` from every note's positional field list.

        ``field_count`` is the number of fields *before* removal; ``sort_idx`` is
        the sort field index *after* removal.
        """
        note_rows = conn.execute(
            "SELECT id, flds FROM notes WHERE mid = ?",
            (ntid,),
        ).fetchall()
        if not note_rows:
            return 0

        updates: list[tuple[str, str, int, int, int]] = []
        for note_row in note_rows:
            values = self._split_fields(str(note_row["flds"] or ""))
            if len(values) < field_count:
                values.extend([""] * (field_count - len(values)))
            del values[removed_ord]
            updates.append(
                (
                    "\x1f".join(values),
                    self._sort_field_value(values, sort_idx),
                    self._field_checksum(values[0] if values else ""),
                    now_sec,
                    int(note_row["id"]),
                )
            )

        conn.executemany(
            """
            UPDATE notes
            SET flds = ?, sfld = ?, csum = ?, mod = ?, usn = -1
            WHERE id = ?
            """,
            updates,
        )
        return len(updates)

    def add_notetype_template(
        self,
        *,
        name: str,
        template_name: str,
        front: str,
        back: str,
    ) -> dict[str, JSONValue]:
        normalized_name = name.strip()
        normalized_template = template_name.strip()
        if not normalized_name or not normalized_template:
            raise ValueError("Notetype and template name are required.")

        with self._connect_write() as conn:
            row = conn.execute(
                "SELECT id FROM notetypes WHERE name = ?",
                (normalized_name,),
            ).fetchone()
            if row is None:
                raise LookupError(f"Notetype not found: {normalized_name}")
            ntid = int(row["id"])

            existing = conn.execute(
                "SELECT 1 FROM templates WHERE ntid = ? AND name = ?",
                (ntid, normalized_template),
            ).fetchone()
            if existing is not None:
                return {"name": normalized_name, "template": normalized_template, "added": False}

            max_ord_row = conn.execute(
                "SELECT COALESCE(MAX(ord), -1) AS max_ord FROM templates WHERE ntid = ?",
                (ntid,),
            ).fetchone()
            next_ord = int(max_ord_row["max_ord"]) + 1
            now_sec = int(time.time())
            conn.execute(
                """
                INSERT INTO templates (ntid, ord, name, mtime_secs, usn, config)
                VALUES (?, ?, ?, ?, -1, ?)
                """,
                (
                    ntid,
                    next_ord,
                    normalized_template,
                    now_sec,
                    bytes(NotetypeTemplateConfig(q_format=front, a_format=back)),
                ),
            )
            conn.execute(
                "UPDATE notetypes SET mtime_secs = ?, usn = -1 WHERE id = ?",
                (now_sec, ntid),
            )
            self._recompute_reqs(conn, ntid)
            self._mark_schema_modified(conn)

        return {
            "name": normalized_name,
            "template": normalized_template,
            "added": True,
            "full_sync_required": True,
        }

    def edit_notetype_template(
        self,
        *,
        name: str,
        template_name: str,
        front: str | None = None,
        back: str | None = None,
    ) -> dict[str, JSONValue]:
        normalized_name = name.strip()
        normalized_template = template_name.strip()
        if not normalized_name or not normalized_template:
            raise ValueError("Notetype and template name are required.")
        if front is None and back is None:
            raise ValueError("Provide at least one of front/back.")

        with self._connect_write() as conn:
            row = conn.execute(
                """
                SELECT t.ntid AS ntid, t.ord AS ord, t.config AS config
                FROM templates AS t
                JOIN notetypes AS n ON n.id = t.ntid
                WHERE n.name = ? AND t.name = ?
                """,
                (normalized_name, normalized_template),
            ).fetchone()
            if row is None:
                raise LookupError(f"Template not found: {normalized_template}")

            ntid = int(row["ntid"])
            ord_ = int(row["ord"])
            cfg = self._decode_template_config(bytes(row["config"] or b""), ntid=ntid, ord_=ord_)
            if front is not None:
                cfg.q_format = front
            if back is not None:
                cfg.a_format = back

            now_sec = int(time.time())
            conn.execute(
                """
                UPDATE templates
                SET mtime_secs = ?, usn = -1, config = ?
                WHERE ntid = ? AND ord = ?
                """,
                (now_sec, bytes(cfg), ntid, ord_),
            )
            # Sync ships notetypes as whole objects keyed off notetypes.usn.
            conn.execute(
                "UPDATE notetypes SET mtime_secs = ?, usn = -1 WHERE id = ?",
                (now_sec, ntid),
            )
            if front is not None:
                self._recompute_reqs(conn, ntid)

        return {"name": normalized_name, "template": normalized_template, "updated": True}

    def set_notetype_css(self, *, name: str, css: str) -> dict[str, JSONValue]:
        normalized = name.strip()
        if not normalized:
            raise ValueError("Notetype name cannot be empty.")

        with self._connect_write() as conn:
            row = conn.execute(
                "SELECT id, config FROM notetypes WHERE name = ?",
                (normalized,),
            ).fetchone()
            if row is None:
                raise LookupError(f"Notetype not found: {normalized}")

            ntid = int(row["id"])
            cfg = self._decode_notetype_config(bytes(row["config"] or b""), ntid=ntid)
            cfg.css = css
            conn.execute(
                "UPDATE notetypes SET mtime_secs = ?, usn = -1, config = ? WHERE id = ?",
                (int(time.time()), bytes(cfg), ntid),
            )

        return {"name": normalized, "updated": True, "css": css}

    # ---- notes ------------------------------------------------------------

    def find_note_ids(self, query: str) -> list[int]:
        compiled = compile_note_query(query, ctx=self._search_context())

        joins_sql = ""
        if compiled.joins:
            joins_sql = "\n            " + "\n            ".join(compiled.joins)

        sql = f"""
            SELECT DISTINCT n.id
            FROM notes AS n{joins_sql}
            WHERE {compiled.where}
            ORDER BY n.id
        """

        with self._connect() as conn:
            rows = conn.execute(sql, compiled.params).fetchall()
        return [int(row["id"]) for row in rows]

    def get_note(self, note_id: int) -> dict[str, JSONValue]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, guid, mid, mod, usn, tags, flds, sfld, csum, flags, data
                FROM notes
                WHERE id = ?
                """,
                (note_id,),
            ).fetchone()

        if row is None:
            raise LookupError(f"Note not found: {note_id}")

        raw_tags = str(row["tags"] or "")
        raw_fields = str(row["flds"] or "")

        return {
            "id": int(row["id"]),
            "guid": str(row["guid"]),
            "mid": int(row["mid"]),
            "mod": int(row["mod"]),
            "usn": int(row["usn"]),
            "tags": self._parse_tags(raw_tags),
            "fields": self._split_fields(raw_fields),
            "sfld": row["sfld"],
            "csum": int(row["csum"]),
            "flags": int(row["flags"]),
            "data": str(row["data"] or ""),
        }

    def get_note_fields(self, *, note_id: int, fields: list[str] | None = None) -> dict[str, str]:
        with self._connect() as conn:
            row = conn.execute("SELECT mid, flds FROM notes WHERE id = ?", (note_id,)).fetchone()
            if row is None:
                raise LookupError(f"Note not found: {note_id}")

            mid = int(row["mid"])
            names = conn.execute(
                "SELECT ord, name FROM fields WHERE ntid = ? ORDER BY ord",
                (mid,),
            ).fetchall()

        values = self._split_fields(str(row["flds"] or ""))
        out: dict[str, str] = {}
        for item in names:
            ord_ = int(item["ord"])
            name = str(item["name"])
            out[name] = values[ord_] if ord_ < len(values) else ""

        if fields:
            wanted = {f.strip() for f in fields if f.strip()}
            return {k: v for k, v in out.items() if k in wanted}
        return out

    def get_tags(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT tags FROM notes").fetchall()

        tags: set[str] = set()
        for row in rows:
            tags.update(self._parse_tags(str(row["tags"] or "")))
        return sorted(tags, key=str.lower)

    # ---- cards ------------------------------------------------------------

    def find_card_ids(self, query: str) -> list[int]:
        compiled = compile_card_query(query, ctx=self._search_context())

        joins_sql = ""
        if compiled.joins:
            joins_sql = "\n            " + "\n            ".join(compiled.joins)

        sql = f"""
            SELECT DISTINCT c.id
            FROM cards AS c{joins_sql}
            WHERE {compiled.where}
            ORDER BY c.id
        """

        with self._connect() as conn:
            rows = conn.execute(sql, compiled.params).fetchall()
        return [int(row["id"]) for row in rows]

    def get_card(self, card_id: int) -> dict[str, JSONValue]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    c.id, c.nid, c.did, c.ord, c.mod, c.usn, c.type, c.queue, c.due,
                    c.ivl, c.factor, c.reps, c.lapses, c.left, c.odue, c.odid,
                    c.flags, c.data,
                    n.mid AS note_mid,
                    nt.name AS notetype_name,
                    n.flds AS note_fields, n.tags AS note_tags,
                    d.name AS deck_name
                FROM cards AS c
                JOIN notes AS n ON n.id = c.nid
                LEFT JOIN notetypes AS nt ON nt.id = n.mid
                LEFT JOIN decks AS d ON d.id = c.did
                WHERE c.id = ?
                """,
                (card_id,),
            ).fetchone()

            timing = self._timing(conn, int(time.time())) if row is not None else None

        if row is None:
            raise LookupError(f"Card not found: {card_id}")

        card_type = int(row["type"])
        queue = int(row["queue"])
        due_raw = int(row["due"])
        left_raw = int(row["left"])
        data_raw = str(row["data"] or "")

        return {
            "cardId": int(row["id"]),
            "note": int(row["nid"]),
            "deckId": int(row["did"]),
            "deckName": str(row["deck_name"] or ""),
            "ord": int(row["ord"]),
            "type": int(row["type"]),
            "queue": int(row["queue"]),
            "due": int(row["due"]),
            "interval": int(row["ivl"]),
            "factor": int(row["factor"]),
            "reps": int(row["reps"]),
            "lapses": int(row["lapses"]),
            "left": int(row["left"]),
            "flags": int(row["flags"]),
            "fields": self._split_fields(str(row["note_fields"] or "")),
            "tags": self._parse_tags(str(row["note_tags"] or "")),
            "data": str(row["data"] or ""),
            "notetype_id": int(row["note_mid"]),
            "notetype_name": str(row["notetype_name"] or ""),
            "due_info": self._decode_due(
                card_type=card_type,
                queue=queue,
                due_raw=due_raw,
                timing=timing,
            ),
            "left_info": self._decode_left(left_raw),
            "data_parsed": self._parse_card_data(data_raw),
        }

    def get_due_counts(self, deck: str | None = None) -> dict[str, int]:
        now_sec = int(time.time())

        with self._connect() as conn:
            today_days = self._timing(conn, now_sec).days_elapsed
            did_filter, params = self._deck_filter(conn, deck)

            new_count = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM cards WHERE queue = 0 {did_filter}",
                    params,
                ).fetchone()[0]
            )
            # queue 1 stores an epoch, queue 3 (day-learn) a day index.
            learn_count = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) FROM cards
                    WHERE ((queue = 1 AND due <= ?) OR (queue = 3 AND due <= ?)) {did_filter}
                    """,
                    (now_sec, today_days, *params),
                ).fetchone()[0]
            )
            review_count = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM cards WHERE queue = 2 AND due <= ? {did_filter}",
                    (today_days, *params),
                ).fetchone()[0]
            )

        return {
            "new": new_count,
            "learn": learn_count,
            "review": review_count,
            "total": new_count + learn_count + review_count,
        }

    def get_next_due_card(self, deck: str | None = None) -> dict[str, JSONValue]:
        now_sec = int(time.time())

        with self._connect() as conn:
            timing = self._timing(conn, now_sec)
            today_days = timing.days_elapsed
            did_filter, params = self._deck_filter(conn, deck)

            # 1) learning/relearning due. Intraday (queue 1) holds an epoch,
            #    day-learn (queue 3) a day index; order both by absolute time.
            day0_epoch = timing.day_start_epoch(0)
            row = conn.execute(
                f"""
                SELECT id, due
                FROM cards
                WHERE ((queue = 1 AND due <= ?) OR (queue = 3 AND due <= ?)) {did_filter}
                ORDER BY CASE WHEN queue = 1 THEN due ELSE ? + due * 86400 END ASC, id ASC
                LIMIT 1
                """,
                (now_sec, today_days, *params, day0_epoch),
            ).fetchone()
            if row is not None:
                return {"card_id": int(row["id"]), "kind": "learn_due"}

            # 2) review due (day index)
            row = conn.execute(
                f"""
                SELECT id, due
                FROM cards
                WHERE queue = 2 AND due <= ? {did_filter}
                ORDER BY due ASC, id ASC
                LIMIT 1
                """,
                (today_days, *params),
            ).fetchone()
            if row is not None:
                return {"card_id": int(row["id"]), "kind": "review_due"}

            # 3) new (position)
            row = conn.execute(
                f"""
                SELECT id, due
                FROM cards
                WHERE queue = 0 {did_filter}
                ORDER BY due ASC, id ASC
                LIMIT 1
                """,
                params,
            ).fetchone()
            if row is not None:
                return {"card_id": int(row["id"]), "kind": "new"}

        return {"card_id": None, "kind": "none"}


    def snapshot_card_state(self, card_id: int) -> dict[str, JSONValue]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    id, did, odid, odue, ord, type, queue, due, ivl, factor, reps, lapses,
                    left, flags, data
                FROM cards
                WHERE id = ?
                """,
                (card_id,),
            ).fetchone()

        if row is None:
            raise LookupError(f"Card not found: {card_id}")

        return {
            "id": int(row["id"]),
            "did": int(row["did"]),
            "odid": int(row["odid"]),
            "odue": int(row["odue"]),
            "ord": int(row["ord"]),
            "type": int(row["type"]),
            "queue": int(row["queue"]),
            "due": int(row["due"]),
            "ivl": int(row["ivl"]),
            "factor": int(row["factor"]),
            "reps": int(row["reps"]),
            "lapses": int(row["lapses"]),
            "left": int(row["left"]),
            "flags": int(row["flags"]),
            "data": str(row["data"] or ""),
        }


    def restore_card_state(self, snapshot: Mapping[str, Any]) -> dict[str, JSONValue]:
        card_id = snapshot.get("id")
        if not isinstance(card_id, int):
            raise ValueError("snapshot.id must be an int")

        now_sec = int(time.time())
        with self._connect_write() as conn:
            updated = conn.execute(
                """
                UPDATE cards
                SET
                    did = ?,
                    odid = ?,
                    odue = ?,
                    ord = ?,
                    type = ?,
                    queue = ?,
                    due = ?,
                    ivl = ?,
                    factor = ?,
                    reps = ?,
                    lapses = ?,
                    left = ?,
                    flags = ?,
                    data = ?,
                    mod = ?,
                    usn = -1
                WHERE id = ?
                """,
                (
                    int(snapshot.get("did") or 0),
                    # Older snapshots predate these keys; a card that was not in a
                    # filtered deck has both at 0.
                    int(snapshot.get("odid") or 0),
                    int(snapshot.get("odue") or 0),
                    int(snapshot.get("ord") or 0),
                    int(snapshot.get("type") or 0),
                    int(snapshot.get("queue") or 0),
                    int(snapshot.get("due") or 0),
                    int(snapshot.get("ivl") or 0),
                    int(snapshot.get("factor") or 0),
                    int(snapshot.get("reps") or 0),
                    int(snapshot.get("lapses") or 0),
                    int(snapshot.get("left") or 0),
                    int(snapshot.get("flags") or 0),
                    str(snapshot.get("data") or ""),
                    now_sec,
                    card_id,
                ),
            ).rowcount

            # Match Anki's undo semantics: delete the exact revlog row the
            # undone review wrote (its id is recorded in the snapshot when the
            # undo entry is pushed) instead of appending a compensating row.
            # Without this the review's own ease 1..4 row survives and
            # _seed_fsrs_card_from_revlog keeps picking it up, so legacy cards
            # seeded from revlog compute a different stability afterwards.
            # usn = -1 restricts the delete to rows that have not synced yet;
            # revlog deletions never propagate to AnkiWeb, so removing a
            # synced row would diverge this collection from the server.
            revlog_id = snapshot.get("revlog_id")
            revlog_deleted = 0
            if isinstance(revlog_id, int) and not isinstance(revlog_id, bool):
                revlog_deleted = conn.execute(
                    "DELETE FROM revlog WHERE id = ? AND cid = ? AND usn = -1",
                    (revlog_id, card_id),
                ).rowcount

        return {
            "card_id": card_id,
            "restored": int(updated) > 0,
            "revlog_deleted": int(revlog_deleted),
        }


    def preview_ratings(self, card_id: int) -> list[dict[str, JSONValue]]:
        review_dt = datetime.now(UTC)

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    id, nid, did, ord, mod, usn, type, queue, due, ivl, factor, reps,
                    lapses, left, odue, odid, flags, data
                FROM cards
                WHERE id = ?
                """,
                (card_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"Card not found: {card_id}")

            timing = self._timing(conn, int(review_dt.timestamp()))

            odid = int(row["odid"])
            if odid != 0 and self._filtered_deck_reschedules(conn, int(row["did"])) is False:
                raise ValueError(
                    "Card is in a preview (non-rescheduling) filtered deck; "
                    "answer it in Anki or empty the deck first."
                )
            # Options come from the home deck when the card is on loan.
            scheduler, _dr, learn_count, relearn_count, params_source = self._build_scheduler_ex(
                conn, odid if odid != 0 else int(row["did"])
            )

            base = self._card_row_to_fsrs(
                row,
                timing=timing,
                now_dt=review_dt,
                learn_step_count=learn_count,
                relearn_step_count=relearn_count,
            )
            if base.last_review is None:
                base.last_review = self._last_review_time(conn, int(row["id"]))

            needs_seed = base.state in (State.Review, State.Relearning) and (
                base.stability is None or base.difficulty is None or base.last_review is None
            )
            if needs_seed:
                seeded = self._seed_fsrs_card_from_revlog(
                    conn,
                    scheduler,
                    card_id=int(row["id"]),
                    now_dt=review_dt,
                )
                if seeded is not None:
                    base.stability = seeded.stability
                    base.difficulty = seeded.difficulty
                    base.last_review = seeded.last_review
                else:
                    ivl_days = int(row["ivl"] or 0)
                    factor = int(row["factor"] or 0)

                    base.stability = float(max(1, ivl_days))
                    if factor > 0:
                        ease_mult = max(1.3, min(3.0, factor / 1000.0))
                        scaled = (ease_mult - 1.3) / (3.0 - 1.3)
                        base.difficulty = max(1.0, min(10.0, 10.0 - (scaled * 9.0)))
                    else:
                        base.difficulty = 5.0

                    mod_sec = int(row["mod"] or int(review_dt.timestamp()))
                    try:
                        base.last_review = datetime.fromtimestamp(mod_sec, tz=UTC)
                    except (OSError, OverflowError, ValueError):
                        base.last_review = review_dt

            if base.state == State.Relearning and base.step is None:
                base.step = 0

            out: list[dict[str, JSONValue]] = []
            for ease in (1, 2, 3, 4):
                # Same seed answer_card will use, so the preview matches the write.
                next_card = self._review_with_fuzz_seed(
                    scheduler,
                    base,
                    Rating(ease),
                    review_datetime=review_dt,
                    card_id=int(row["id"]),
                    reps=int(row["reps"]),
                )

                (
                    new_type,
                    new_queue,
                    new_due,
                    new_ivl,
                    new_left,
                    next_due_epoch,
                ) = self._map_fsrs_result_to_anki(
                    current_row=row,
                    next_card=next_card,
                    timing=timing,
                    learn_step_count=learn_count,
                    relearn_step_count=relearn_count,
                    now_dt=review_dt,
                )

                out.append(
                    {
                        "ease": ease,
                        "fsrs_params": params_source,
                        "type": new_type,
                        "queue": new_queue,
                        "due": new_due,
                        "interval": new_ivl,
                        "left": new_left,
                        "next_due_epoch_secs": next_due_epoch,
                        "due_info": self._decode_due(
                            card_type=new_type,
                            queue=new_queue,
                            due_raw=new_due,
                            timing=timing,
                        ),
                        "state": str(next_card.state),
                    }
                )

            return out

    def move_cards(self, *, card_ids: list[int], deck: str) -> dict[str, JSONValue]:
        ids = sorted({int(cid) for cid in card_ids if int(cid) > 0})
        if not ids:
            return {"moved": 0, "card_ids": []}
        with self._connect_write() as conn:
            did = self._resolve_deck_id(conn, deck)
            if self._filtered_deck_reschedules(conn, did) is not None:
                # rslib FilteredDeckError::CanNotMoveCardsInto
                raise ValueError(f"Cannot move cards into a filtered deck: {deck}")
            placeholders = ", ".join(["?"] * len(ids))
            # A card leaving a filtered deck first restores its real schedule
            # (rslib Card::set_deck -> remove_from_filtered_deck_restoring_queue).
            updated = conn.execute(
                f"""
                UPDATE cards
                SET due = {RESTORED_DUE_IF_ON_LOAN_SQL},
                    queue = CASE
                        WHEN odid = 0 OR queue < 0 THEN queue
                        ELSE {queue_from_type_sql()}
                    END,
                    odid = 0,
                    odue = 0,
                    did = ?,
                    mod = ?,
                    usn = -1
                WHERE id IN ({placeholders})
                """,
                (did, int(time.time()), *ids),
            ).rowcount
        return {"moved": int(updated), "card_ids": ids, "deck": deck}

    def set_card_flag(self, *, card_ids: list[int], flag: int) -> dict[str, JSONValue]:
        if flag < 0 or flag > 7:
            raise ValueError("flag must be in range 0..7")
        ids = sorted({int(cid) for cid in card_ids if int(cid) > 0})
        if not ids:
            return {"updated": 0, "card_ids": []}
        with self._connect_write() as conn:
            placeholders = ", ".join(["?"] * len(ids))
            updated = conn.execute(
                f"UPDATE cards SET flags = ?, mod = ?, usn = -1 WHERE id IN ({placeholders})",
                (flag, int(time.time()), *ids),
            ).rowcount
        return {"updated": int(updated), "card_ids": ids, "flag": flag}

    def bury_cards(self, *, card_ids: list[int]) -> dict[str, JSONValue]:
        ids = sorted({int(cid) for cid in card_ids if int(cid) > 0})
        if not ids:
            return {"buried": 0, "card_ids": []}
        with self._connect_write() as conn:
            placeholders = ", ".join(["?"] * len(ids))
            updated = conn.execute(
                f"UPDATE cards SET queue = -2, mod = ?, usn = -1 WHERE id IN ({placeholders})",
                (int(time.time()), *ids),
            ).rowcount
        return {"buried": int(updated), "card_ids": ids}

    def unbury_cards(self, *, deck: str | None = None) -> dict[str, JSONValue]:
        with self._connect_write() as conn:
            now_sec = int(time.time())
            if deck is None:
                updated = conn.execute(
                    f"""
                    UPDATE cards
                    SET queue = {queue_from_type_sql()},
                    mod = ?, usn = -1
                    WHERE queue IN (-2, -3)
                    """,
                    (now_sec,),
                ).rowcount
                return {"unburied": int(updated), "scope": "all"}

            rows = conn.execute(
                "SELECT id FROM decks WHERE name = ? OR name LIKE ?",
                (deck.strip(), f"{deck.strip()}::%"),
            ).fetchall()
            dids = [int(r["id"]) for r in rows]
            if not dids:
                return {"unburied": 0, "deck": deck}

            placeholders = ", ".join(["?"] * len(dids))
            updated = conn.execute(
                f"""
                UPDATE cards
                SET queue = {queue_from_type_sql()},
                mod = ?, usn = -1
                WHERE queue IN (-2, -3) AND did IN ({placeholders})
                """,
                (now_sec, *dids),
            ).rowcount
            return {"unburied": int(updated), "deck": deck}

    def reschedule_cards(self, *, card_ids: list[int], days: int) -> dict[str, JSONValue]:
        if days < 0:
            raise ValueError("days must be >= 0")
        ids = sorted({int(cid) for cid in card_ids if int(cid) > 0})
        if not ids:
            return {"rescheduled": 0, "card_ids": []}

        with self._connect_write() as conn:
            today = self._timing(conn, int(time.time())).days_elapsed
            target_due = today + days
            placeholders = ", ".join(["?"] * len(ids))
            updated = conn.execute(
                f"""
                UPDATE cards
                SET type = 2, queue = 2, due = ?, ivl = ?, {LEAVE_FILTERED_DECK_SQL},
                    mod = ?, usn = -1
                WHERE id IN ({placeholders})
                """,
                (target_due, max(1, days), int(time.time()), *ids),
            ).rowcount
        return {"rescheduled": int(updated), "card_ids": ids, "days": days}

    def reset_cards(self, *, card_ids: list[int]) -> dict[str, JSONValue]:
        ids = sorted({int(cid) for cid in card_ids if int(cid) > 0})
        if not ids:
            return {"reset": 0, "card_ids": []}

        with self._connect_write() as conn:
            next_due = self._next_new_due(conn)
            now_sec = int(time.time())
            updated = 0
            for offset, cid in enumerate(ids):
                changed = conn.execute(
                    f"""
                    UPDATE cards
                    SET type = 0, queue = 0, due = ?, ivl = 0, factor = 0, reps = 0, lapses = 0,
                        left = 0, {LEAVE_FILTERED_DECK_SQL}, data = '{{}}', mod = ?, usn = -1
                    WHERE id = ?
                    """,
                    (next_due + offset, now_sec, cid),
                ).rowcount
                updated += int(changed)
        return {"reset": updated, "card_ids": ids}

    # ---- write paths ------------------------------------------------------

    def create_deck(self, name: str) -> dict[str, JSONValue]:
        return self.write_deck(name=name)

    def rename_deck(self, *, old_name: str, new_name: str) -> dict[str, JSONValue]:
        source = old_name.strip()
        target = new_name.strip()
        if not source or not target:
            raise ValueError("Deck names cannot be empty.")
        if source == target:
            return {
                "from": source,
                "to": target,
                "renamed_decks": 0,
                "unchanged": True,
                "items": [],
            }

        with self._connect_write() as conn:
            rows = conn.execute(
                """
                SELECT id, name
                FROM decks
                WHERE name = ? OR name LIKE ?
                ORDER BY LENGTH(name), name
                """,
                (source, f"{source}::%"),
            ).fetchall()
            if not rows:
                raise LookupError(f"Deck not found: {source}")

            scoped_ids = {int(row["id"]) for row in rows}
            conflict = conn.execute(
                """
                SELECT id, name
                FROM decks
                WHERE (name = ? OR name LIKE ?)
                LIMIT 1
                """,
                (target, f"{target}::%"),
            ).fetchone()
            if conflict is not None and int(conflict["id"]) not in scoped_ids:
                raise ValueError(f"Target deck path already exists: {target}")

            now_sec = int(time.time())
            temp_prefix = f"__anki_cli_tmp_{int(time.time() * 1000)}__"
            plan: list[tuple[int, str, str, str]] = []
            for row in rows:
                did = int(row["id"])
                current_name = str(row["name"])
                suffix = current_name[len(source) :]
                temp_name = f"{temp_prefix}{suffix}"
                final_name = f"{target}{suffix}"
                plan.append((did, current_name, temp_name, final_name))

            for did, _from_name, temp_name, _to_name in plan:
                conn.execute(
                    "UPDATE decks SET name = ?, mtime_secs = ?, usn = -1 WHERE id = ?",
                    (temp_name, now_sec, did),
                )

            items: list[dict[str, JSONValue]] = []
            for did, from_name, _temp_name, to_name in plan:
                conn.execute(
                    "UPDATE decks SET name = ?, mtime_secs = ?, usn = -1 WHERE id = ?",
                    (to_name, now_sec, did),
                )
                items.append({"id": did, "from": from_name, "to": to_name})

        return {
            "from": source,
            "to": target,
            "renamed_decks": len(items),
            "items": items,
        }

    def write_deck(
        self,
        *,
        name: str,
        deck_id: int | None = None,
        config_id: int | None = None,
        description: str | None = None,
    ) -> dict[str, JSONValue]:
        normalized = name.strip()
        if not normalized:
            raise ValueError("Deck name cannot be empty.")

        with self._connect_write() as conn:
            target_row: sqlite3.Row | None = None
            if deck_id is not None:
                target_row = conn.execute(
                    "SELECT id, common, kind FROM decks WHERE id = ?",
                    (deck_id,),
                ).fetchone()
                if target_row is None:
                    raise LookupError(f"Deck not found: {deck_id}")
            else:
                target_row = conn.execute(
                    "SELECT id, common, kind FROM decks WHERE name = ?",
                    (normalized,),
                ).fetchone()

            template = conn.execute(
                "SELECT common, kind FROM decks WHERE name = ? LIMIT 1",
                ("Default",),
            ).fetchone()
            if template is None:
                template = conn.execute(
                    "SELECT common, kind FROM decks ORDER BY id LIMIT 1"
                ).fetchone()
            if template is None:
                raise RuntimeError("No deck template row available to create a deck.")

            common_blob = (
                bytes(target_row["common"])
                if target_row is not None
                else bytes(template["common"])
            )
            kind_blob = (
                bytes(target_row["kind"])
                if target_row is not None
                else bytes(template["kind"])
            )
            decode_did = int(target_row["id"]) if target_row else -1
            deck_common = self._decode_deck_common(common_blob, did=decode_did)
            deck_kind = self._decode_deck_kind(kind_blob, did=decode_did)

            kind_name, kind_msg = betterproto.which_one_of(deck_kind, "kind")
            if kind_name == "normal" and kind_msg is not None:
                if config_id is not None:
                    kind_msg.config_id = int(config_id)
                if description is not None:
                    kind_msg.description = description
            elif kind_name == "":
                from anki_cli.proto.anki.decks import DeckNormal

                deck_kind.normal = DeckNormal(
                    config_id=int(config_id or 1),
                    description=description or "",
                )

            now_sec = int(time.time())
            if target_row is None:
                assigned_id = self._allocate_row_id(conn, "decks")
                conn.execute(
                    """
                    INSERT INTO decks (id, name, mtime_secs, usn, common, kind)
                    VALUES (?, ?, ?, -1, ?, ?)
                    """,
                    (
                        assigned_id,
                        normalized,
                        now_sec,
                        bytes(deck_common),
                        bytes(deck_kind),
                    ),
                )
                return {"deck": normalized, "id": assigned_id, "created": True}

            assigned_id = int(target_row["id"])
            conn.execute(
                """
                UPDATE decks
                SET name = ?, mtime_secs = ?, usn = -1, common = ?, kind = ?
                WHERE id = ?
                """,
                (
                    normalized,
                    now_sec,
                    bytes(deck_common),
                    bytes(deck_kind),
                    assigned_id,
                ),
            )
            return {"deck": normalized, "id": assigned_id, "created": False, "updated": True}

    def delete_deck(self, name: str) -> dict[str, JSONValue]:
        normalized = name.strip()
        if not normalized:
            raise ValueError("Deck name cannot be empty.")

        with self._connect_write() as conn:
            deck_rows = conn.execute(
                """
                SELECT id, name, kind
                FROM decks
                WHERE name = ? OR name LIKE ?
                ORDER BY id
                """,
                (normalized, f"{normalized}::%"),
            ).fetchall()
            if not deck_rows:
                return {
                    "deck": normalized,
                    "deleted": False,
                    "deleted_decks": 0,
                    "deleted_notes": 0,
                    "deleted_cards": 0,
                    "returned_cards": 0,
                }

            deck_ids = [int(row["id"]) for row in deck_rows]
            if 1 in deck_ids:
                raise ValueError("Cannot delete the Default deck.")

            # Filtered decks only borrow cards; deleting one must send the cards
            # back to their home deck (did = odid, due = odue) rather than delete
            # them. Normal decks own their cards, including any currently on loan
            # to a filtered deck (odid = this deck).
            filtered_ids: list[int] = []
            normal_ids: list[int] = []
            for row in deck_rows:
                did = int(row["id"])
                kind = self._decode_deck_kind(bytes(row["kind"] or b""), did=did)
                kind_name, _ = betterproto.which_one_of(kind, "kind")
                if kind_name == "filtered":
                    filtered_ids.append(did)
                elif kind_name == "normal":
                    normal_ids.append(did)
                else:
                    # A destructive dispatch must fail closed on a malformed blob.
                    raise ValueError(
                        f"Deck {did} ({row['name']}) has an unknown kind; refusing to delete."
                    )

            now_sec = int(time.time())
            returned_cards = 0
            if filtered_ids:
                returned_cards = self._return_cards_from_filtered_decks(
                    conn, filtered_ids, now_sec=now_sec
                )

            deleted_cards = 0
            deleted_notes = 0
            if normal_ids:
                deleted_cards, deleted_notes = self._delete_cards_owned_by_decks(
                    conn, normal_ids
                )

            deck_placeholders = ", ".join(["?"] * len(deck_ids))
            deleted_decks = int(
                conn.execute(
                    f"DELETE FROM decks WHERE id IN ({deck_placeholders})",
                    tuple(deck_ids),
                ).rowcount
            )
            self._insert_graves(conn, deck_ids, grave_type=2)

        return {
            "deck": normalized,
            "deleted": deleted_decks > 0,
            "deleted_decks": deleted_decks,
            "deleted_notes": deleted_notes,
            "deleted_cards": deleted_cards,
            "returned_cards": returned_cards,
        }

    def _delete_cards_owned_by_decks(
        self,
        conn: sqlite3.Connection,
        deck_ids: list[int],
    ) -> tuple[int, int]:
        """Delete every card owned by ``deck_ids`` plus notes left with no cards.

        A card is owned by a deck if it lives there (``did``) or is on loan from
        there to a filtered deck (``odid``). Ids are staged in temp tables so the
        graves come from the same set that was deleted, and so we never build an
        ``IN (?, ?, ...)`` list that could exceed SQLite's variable limit.

        Returns ``(deleted_cards, deleted_notes)``.
        """
        placeholders = ", ".join(["?"] * len(deck_ids))
        # The explicit `odid != 0` is redundant for correctness (deck ids are never
        # 0) but lets SQLite use Anki's partial index `idx_cards_odid ... WHERE
        # odid != 0` via MULTI-INDEX OR instead of scanning the cards table.
        scope = f"(did IN ({placeholders}) OR (odid != 0 AND odid IN ({placeholders})))"
        scope_params = (*deck_ids, *deck_ids)

        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _del_cids (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _del_nids (id INTEGER PRIMARY KEY)")
        conn.execute("DELETE FROM _del_cids")
        conn.execute("DELETE FROM _del_nids")

        conn.execute(
            f"INSERT INTO _del_cids SELECT id FROM cards WHERE {scope}",
            scope_params,
        )
        # Notes whose every card is being deleted are orphaned and go too.
        conn.execute(
            """
            INSERT INTO _del_nids
            SELECT nid
            FROM cards
            WHERE nid IN (SELECT nid FROM cards WHERE id IN (SELECT id FROM _del_cids))
            GROUP BY nid
            HAVING SUM(CASE WHEN id IN (SELECT id FROM _del_cids) THEN 0 ELSE 1 END) = 0
            """
        )

        deleted_notes = int(
            conn.execute("DELETE FROM notes WHERE id IN (SELECT id FROM _del_nids)").rowcount
        )
        deleted_cards = int(
            conn.execute("DELETE FROM cards WHERE id IN (SELECT id FROM _del_cids)").rowcount
        )
        conn.execute(
            "INSERT OR IGNORE INTO graves (oid, type, usn) SELECT id, 0, -1 FROM _del_cids"
        )
        conn.execute(
            "INSERT OR IGNORE INTO graves (oid, type, usn) SELECT id, 1, -1 FROM _del_nids"
        )
        conn.execute("DELETE FROM _del_cids")
        conn.execute("DELETE FROM _del_nids")
        return deleted_cards, deleted_notes

    def _return_cards_from_filtered_decks(
        self,
        conn: sqlite3.Connection,
        filtered_deck_ids: list[int],
        *,
        now_sec: int,
    ) -> int:
        """Move cards out of the given filtered decks back to their home decks.

        Mirrors Anki's ``remove_from_filtered_deck_restoring_queue``: restore
        ``did``/``due`` from ``odid``/``odue`` and recompute ``queue`` from the
        card type. Suspended and buried cards keep their negative queue.
        """
        placeholders = ", ".join(["?"] * len(filtered_deck_ids))
        # SQLite evaluates SET expressions against the pre-update row, so the
        # restored due value has to be spelled out again inside the queue CASE.
        restored_due = RESTORED_DUE_SQL
        cursor = conn.execute(
            f"""
            UPDATE cards
            SET did = odid,
                due = {restored_due},
                odid = 0,
                odue = 0,
                queue = CASE
                    WHEN queue < 0 THEN queue
                    ELSE {queue_from_type_sql(due_expr=restored_due)}
                END,
                mod = ?,
                usn = -1
            WHERE did IN ({placeholders}) AND odid != 0
            """,
            (now_sec, *filtered_deck_ids),
        )
        returned = int(cursor.rowcount)

        # A card in a filtered deck with no home deck recorded is malformed;
        # rather than leave it pointing at a deck that no longer exists, park it
        # in Default (the same recovery Anki's Check Database performs).
        stray = conn.execute(
            f"""
            UPDATE cards
            SET did = 1, mod = ?, usn = -1
            WHERE did IN ({placeholders}) AND odid = 0
            """,
            (now_sec, *filtered_deck_ids),
        )
        return returned + int(stray.rowcount)

    def set_deck_config(
        self,
        *,
        name: str,
        updates: dict[str, JSONValue],
    ) -> dict[str, JSONValue]:
        normalized = name.strip()
        if not normalized:
            raise ValueError("Deck name cannot be empty.")
        if not updates:
            return {"deck": normalized, "updated": False, "config": {}}

        with self._connect_write() as conn:
            row = conn.execute(
                "SELECT id, kind FROM decks WHERE name = ?",
                (normalized,),
            ).fetchone()
            if row is None:
                raise LookupError(f"Deck not found: {normalized}")

            did = int(row["id"])
            kind = self._decode_deck_kind(bytes(row["kind"] or b""), did=did)
            kind_name, kind_msg = betterproto.which_one_of(kind, "kind")
            if kind_name != "normal" or kind_msg is None:
                raise ValueError(f"Deck '{normalized}' is not a normal deck.")

            config_id = int(kind_msg.config_id)
            cfg_row = conn.execute(
                "SELECT config FROM deck_config WHERE id = ?",
                (config_id,),
            ).fetchone()
            if cfg_row is None:
                raise LookupError(f"Deck config not found: {config_id}")

            cfg = self._decode_deck_config(bytes(cfg_row["config"] or b""), dcid=config_id)
            applied: dict[str, JSONValue] = {}
            for key, value in updates.items():
                low_key = key.strip().lower()
                if low_key == "new_per_day":
                    parsed = self._coerce_int_value(value)
                    if parsed is None:
                        raise ValueError("new_per_day must be an integer.")
                    cfg.new_per_day = parsed
                    applied["new_per_day"] = parsed
                elif low_key == "reviews_per_day":
                    parsed = self._coerce_int_value(value)
                    if parsed is None:
                        raise ValueError("reviews_per_day must be an integer.")
                    cfg.reviews_per_day = parsed
                    applied["reviews_per_day"] = parsed
                elif low_key == "desired_retention":
                    parsed = self._coerce_float_value(value)
                    if parsed is None:
                        raise ValueError("desired_retention must be a float.")
                    cfg.desired_retention = parsed
                    applied["desired_retention"] = parsed
                elif low_key == "maximum_review_interval":
                    parsed = self._coerce_int_value(value)
                    if parsed is None:
                        raise ValueError("maximum_review_interval must be an integer.")
                    cfg.maximum_review_interval = parsed
                    applied["maximum_review_interval"] = parsed
                elif low_key == "learn_steps":
                    parsed = self._coerce_float_list(value)
                    cfg.learn_steps = parsed
                    applied["learn_steps"] = parsed
                elif low_key == "relearn_steps":
                    parsed = self._coerce_float_list(value)
                    cfg.relearn_steps = parsed
                    applied["relearn_steps"] = parsed
                else:
                    raise ValueError(f"Unsupported deck config key: {key}")

            conn.execute(
                """
                UPDATE deck_config
                SET mtime_secs = ?, usn = -1, config = ?
                WHERE id = ?
                """,
                (int(time.time()), bytes(cfg), config_id),
            )

        return {
            "deck": normalized,
            "updated": bool(applied),
            "config_id": config_id,
            "applied": applied,
            "config": self._deck_config_to_dict(cfg),
        }

    def add_note(
        self,
        *,
        deck: str,
        notetype: str,
        fields: dict[str, str],
        tags: list[str] | None,
        allow_duplicate: bool,
    ) -> int:
        with self._connect_write() as conn:
            deck_id = self._resolve_deck_id(conn, deck)
            notetype_id, field_names, sort_field_idx, is_cloze = self._load_notetype_schema(
                conn, notetype
            )

            ordered_values: list[str] = []
            for field_name in field_names:
                if field_name not in fields:
                    raise LookupError(f"Missing field '{field_name}' for notetype '{notetype}'.")
                ordered_values.append(str(fields[field_name]))

            # rslib note_fields_check works on the first field with markup
            # stripped: Empty is checked before Duplicate and is not lifted by
            # allow_duplicate, so "<br>", "&nbsp;" and "<b></b>" are all empty.
            first_stripped = _strip_html_preserving_media_filenames(
                ordered_values[0] if ordered_values else ""
            )
            if not first_stripped.strip():
                raise EmptyNoteError(
                    notetype=notetype, field_name=field_names[0] if field_names else ""
                )
            csum = self._checksum_of_stripped(first_stripped)

            if not allow_duplicate:
                dup_ids = self._find_duplicate_note_ids(
                    conn, notetype_id=notetype_id, csum=csum, first_stripped=first_stripped
                )
                if dup_ids:
                    raise DuplicateNoteError(notetype=notetype, duplicate_ids=dup_ids)

            note_id = self._allocate_row_id(conn, "notes")
            now_sec = int(time.time())
            sfld = self._sort_field_value(ordered_values, sort_field_idx)
            flds = "\x1f".join(ordered_values)
            tag_text = self._format_tags(tags or [])

            conn.execute(
                """
                INSERT INTO notes (id, guid, mid, mod, usn, tags, flds, sfld, csum, flags, data)
                VALUES (?, ?, ?, ?, -1, ?, ?, ?, ?, 0, '')
                """,
                (
                    note_id,
                    self._build_guid(note_id),
                    notetype_id,
                    now_sec,
                    tag_text,
                    flds,
                    sfld,
                    csum,
                ),
            )

            template_ords = self._template_ords_for_note(
                conn,
                notetype_id,
                ordered_values,
                is_cloze,
                field_names=field_names,
                has_tags=bool(tag_text.strip()),
                ensure_not_empty=True,
            )
            self._insert_new_cards(
                conn, note_id=note_id, deck_id=deck_id, ords=template_ords, now_sec=now_sec
            )

            return note_id

    def _insert_new_cards(
        self,
        conn: sqlite3.Connection,
        *,
        note_id: int,
        deck_id: int,
        ords: list[int],
        now_sec: int,
    ) -> None:
        if not ords:
            return
        next_due = self._next_new_due(conn)
        for offset, ord_value in enumerate(ords):
            conn.execute(
                """
                INSERT INTO cards (
                    id, nid, did, ord, mod, usn, type, queue, due, ivl, factor, reps,
                    lapses, left, odue, odid, flags, data
                )
                VALUES (?, ?, ?, ?, ?, -1, 0, 0, ?, 0, 0, 0, 0, 0, 0, 0, 0, '{}')
                """,
                (
                    self._allocate_row_id(conn, "cards"),
                    note_id,
                    deck_id,
                    ord_value,
                    now_sec,
                    next_due + offset,
                ),
            )

    def _generate_missing_cards(
        self,
        conn: sqlite3.Connection,
        *,
        note_id: int,
        notetype_id: int,
        field_values: list[str],
        field_names: list[str],
        has_tags: bool,
        now_sec: int,
    ) -> list[int]:
        """Cards a template now renders for but the note lacks (rslib
        ``generate_cards_for_existing_note``). Existing cards are never removed;
        Anki leaves that to Empty Cards. New cards go to the deck of the note's
        existing cards (home deck if one is on loan)."""
        existing_rows = conn.execute(
            "SELECT ord, did, odid FROM cards WHERE nid = ? ORDER BY ord", (note_id,)
        ).fetchall()
        existing = {int(r["ord"]) for r in existing_rows}
        nt_row = conn.execute(
            "SELECT config FROM notetypes WHERE id = ?", (notetype_id,)
        ).fetchone()
        is_cloze = False
        if nt_row is not None:
            is_cloze = int(
                self._decode_notetype_config(bytes(nt_row["config"] or b""), ntid=notetype_id).kind
            ) == 1
        wanted = self._template_ords_for_note(
            conn,
            notetype_id,
            field_values,
            is_cloze,
            field_names=field_names,
            has_tags=has_tags,
            ensure_not_empty=False,
        )
        missing = sorted(set(wanted) - existing)
        if not missing:
            return []
        if existing_rows:
            first = existing_rows[0]
            deck_id = int(first["odid"]) if int(first["odid"]) != 0 else int(first["did"])
        else:
            deck_id = 1
        self._insert_new_cards(
            conn, note_id=note_id, deck_id=deck_id, ords=missing, now_sec=now_sec
        )
        return missing

    def add_notes(
        self,
        notes: list[dict[str, JSONValue]],
        *,
        allow_duplicate: bool = False,
    ) -> list[int | None]:
        """AnkiConnect ``addNotes`` shape: one id per item, ``None`` for a refused one.

        Only *per-item* problems become ``None`` — a duplicate or empty note
        (``NoteRejectedError``) or a deck/notetype/field that does not exist
        (``LookupError``). Anything else (collection locked, corrupt notetype
        config, ...) would fail every item identically, so it propagates and the
        whole call fails instead of reporting N spurious per-item failures.

        Each item is its own transaction, so without ``allow_duplicate`` the second
        of two identical items in one batch is refused as a duplicate of the first.
        """
        output: list[int | None] = []
        for item in notes:
            deck = str(item.get("deck") or item.get("deckName") or "").strip()
            notetype = str(item.get("notetype") or item.get("modelName") or "").strip()
            raw_fields = item.get("fields")
            raw_tags = item.get("tags")

            if not deck or not notetype or not isinstance(raw_fields, dict):
                output.append(None)
                continue

            try:
                note_id = self.add_note(
                    deck=deck,
                    notetype=notetype,
                    fields={str(k): str(v) for k, v in raw_fields.items()},
                    tags=self._coerce_tags(raw_tags),
                    allow_duplicate=allow_duplicate,
                )
            except (NoteRejectedError, LookupError):
                output.append(None)
            else:
                output.append(note_id)
        return output

    def update_note(
        self,
        *,
        note_id: int,
        fields: dict[str, str] | None,
        tags: list[str] | None,
    ) -> dict[str, JSONValue]:
        with self._connect_write() as conn:
            row = conn.execute(
                "SELECT id, mid, tags, flds FROM notes WHERE id = ?",
                (note_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"Note not found: {note_id}")

            notetype_id = int(row["mid"])
            field_names, sort_idx = self._field_schema_for_mid(conn, notetype_id)
            current_values = self._split_fields(str(row["flds"] or ""))
            if len(current_values) < len(field_names):
                current_values.extend([""] * (len(field_names) - len(current_values)))

            updated_fields = False
            updated_tags = False
            now_sec = int(time.time())

            if fields:
                name_to_ord = {name: idx for idx, name in enumerate(field_names)}
                for key, value in fields.items():
                    if key not in name_to_ord:
                        raise LookupError(
                            f"Field '{key}' does not exist in notetype {notetype_id}."
                        )
                    current_values[name_to_ord[key]] = str(value)

                conn.execute(
                    """
                    UPDATE notes
                    SET flds = ?, sfld = ?, csum = ?, mod = ?, usn = -1
                    WHERE id = ?
                    """,
                    (
                        "\x1f".join(current_values),
                        self._sort_field_value(current_values, sort_idx),
                        self._field_checksum(current_values[0] if current_values else ""),
                        now_sec,
                        note_id,
                    ),
                )
                updated_fields = True

            tag_text = str(row["tags"] or "")
            if tags is not None:
                tag_text = self._format_tags(tags)
                conn.execute(
                    "UPDATE notes SET tags = ?, mod = ?, usn = -1 WHERE id = ?",
                    (tag_text, now_sec, note_id),
                )
                updated_tags = True

            generated_cards: list[int] = []
            if updated_fields or updated_tags:
                generated_cards = self._generate_missing_cards(
                    conn,
                    note_id=note_id,
                    notetype_id=notetype_id,
                    field_values=current_values,
                    field_names=field_names,
                    has_tags=bool(tag_text.strip()),
                    now_sec=now_sec,
                )

        return {
            "note_id": note_id,
            "updated_fields": updated_fields,
            "updated_tags": updated_tags,
            "generated_cards": generated_cards,
        }

    def delete_notes(self, note_ids: list[int]) -> dict[str, JSONValue]:
        normalized_ids = sorted({int(nid) for nid in note_ids if int(nid) > 0})
        if not normalized_ids:
            return {
                "requested": 0,
                "deleted_notes": 0,
                "deleted_cards": 0,
                "missing_note_ids": [],
            }

        with self._connect_write() as conn:
            placeholders = ", ".join(["?"] * len(normalized_ids))
            existing_rows = conn.execute(
                f"SELECT id FROM notes WHERE id IN ({placeholders})",
                tuple(normalized_ids),
            ).fetchall()
            existing_ids = [int(row["id"]) for row in existing_rows]
            if not existing_ids:
                return {
                    "requested": len(normalized_ids),
                    "deleted_notes": 0,
                    "deleted_cards": 0,
                    "missing_note_ids": normalized_ids,
                }

            existing_set = set(existing_ids)
            missing_ids = [nid for nid in normalized_ids if nid not in existing_set]
            note_placeholders = ", ".join(["?"] * len(existing_ids))

            card_rows = conn.execute(
                f"SELECT id FROM cards WHERE nid IN ({note_placeholders})",
                tuple(existing_ids),
            ).fetchall()
            card_ids = [int(row["id"]) for row in card_rows]

            self._insert_graves(conn, card_ids, grave_type=0)
            self._insert_graves(conn, existing_ids, grave_type=1)

            deleted_cards = int(
                conn.execute(
                    f"DELETE FROM cards WHERE nid IN ({note_placeholders})",
                    tuple(existing_ids),
                ).rowcount
            )
            deleted_notes = int(
                conn.execute(
                    f"DELETE FROM notes WHERE id IN ({note_placeholders})",
                    tuple(existing_ids),
                ).rowcount
            )

        return {
            "requested": len(normalized_ids),
            "deleted_notes": deleted_notes,
            "deleted_cards": deleted_cards,
            "missing_note_ids": missing_ids,
        }

    def delete_card(self, card_id: int) -> dict[str, JSONValue]:
        if card_id <= 0:
            return {"card_id": card_id, "deleted": False}

        with self._connect_write() as conn:
            row = conn.execute("SELECT id FROM cards WHERE id = ?", (card_id,)).fetchone()
            if row is None:
                return {"card_id": card_id, "deleted": False}

            deleted = int(
                conn.execute("DELETE FROM cards WHERE id = ?", (card_id,)).rowcount
            )
            self._insert_graves(conn, [card_id], grave_type=0)

        return {"card_id": card_id, "deleted": deleted > 0}

    def suspend_cards(self, card_ids: list[int]) -> dict[str, JSONValue]:
        return self._set_cards_suspended(card_ids, suspended=True)

    def unsuspend_cards(self, card_ids: list[int]) -> dict[str, JSONValue]:
        return self._set_cards_suspended(card_ids, suspended=False)

    def add_tags(self, note_ids: list[int], tags: list[str]) -> dict[str, JSONValue]:
        normalized_note_ids = sorted({int(nid) for nid in note_ids if int(nid) > 0})
        normalized_tags = self._coerce_tags(tags)
        if not normalized_note_ids or not normalized_tags:
            return {"updated": 0, "note_ids": [], "tags": normalized_tags}

        lower_to_canonical = {tag.lower(): tag for tag in normalized_tags}

        with self._connect_write() as conn:
            placeholders = ", ".join(["?"] * len(normalized_note_ids))
            rows = conn.execute(
                f"SELECT id, tags FROM notes WHERE id IN ({placeholders})",
                tuple(normalized_note_ids),
            ).fetchall()

            now_sec = int(time.time())
            updates: list[tuple[str, int, int]] = []
            for row in rows:
                existing = self._parse_tags(str(row["tags"] or ""))
                merged: dict[str, str] = {tag.lower(): tag for tag in existing}
                merged.update(lower_to_canonical)
                merged_tags = [merged[key] for key in sorted(merged)]
                updates.append((self._format_tags(merged_tags), now_sec, int(row["id"])))

            conn.executemany(
                "UPDATE notes SET tags = ?, mod = ?, usn = -1 WHERE id = ?",
                updates,
            )

        return {
            "updated": len(updates),
            "note_ids": [entry[2] for entry in updates],
            "tags": normalized_tags,
        }

    def remove_tags(self, note_ids: list[int], tags: list[str]) -> dict[str, JSONValue]:
        normalized_note_ids = sorted({int(nid) for nid in note_ids if int(nid) > 0})
        normalized_tags = self._coerce_tags(tags)
        if not normalized_note_ids or not normalized_tags:
            return {"updated": 0, "note_ids": [], "tags": normalized_tags}

        removals = {tag.lower() for tag in normalized_tags}

        with self._connect_write() as conn:
            placeholders = ", ".join(["?"] * len(normalized_note_ids))
            rows = conn.execute(
                f"SELECT id, tags FROM notes WHERE id IN ({placeholders})",
                tuple(normalized_note_ids),
            ).fetchall()

            now_sec = int(time.time())
            updates: list[tuple[str, int, int]] = []
            for row in rows:
                existing = self._parse_tags(str(row["tags"] or ""))
                kept = [tag for tag in existing if tag.lower() not in removals]
                updates.append((self._format_tags(kept), now_sec, int(row["id"])))

            conn.executemany(
                "UPDATE notes SET tags = ?, mod = ?, usn = -1 WHERE id = ?",
                updates,
            )

        return {
            "updated": len(updates),
            "note_ids": [entry[2] for entry in updates],
            "tags": normalized_tags,
        }

    def get_tag_counts(self) -> list[dict[str, JSONValue]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT tags FROM notes").fetchall()

        counts: dict[str, int] = {}
        for row in rows:
            for tag in self._parse_tags(str(row["tags"] or "")):
                counts[tag] = counts.get(tag, 0) + 1

        return [{"tag": tag, "count": counts[tag]} for tag in sorted(counts, key=str.lower)]

    def rename_tag(self, *, old_tag: str, new_tag: str) -> dict[str, JSONValue]:
        source = old_tag.strip()
        target = new_tag.strip()
        if not source or not target:
            raise ValueError("Both tags are required.")

        with self._connect_write() as conn:
            rows = conn.execute("SELECT id, tags FROM notes").fetchall()
            updates: list[tuple[str, int, int]] = []
            now_sec = int(time.time())

            for row in rows:
                tags = self._parse_tags(str(row["tags"] or ""))
                if source not in tags:
                    continue
                merged = [target if t == source else t for t in tags]
                merged = sorted(set(merged), key=str.lower)
                updates.append((self._format_tags(merged), now_sec, int(row["id"])))

            if updates:
                conn.executemany(
                    "UPDATE notes SET tags = ?, mod = ?, usn = -1 WHERE id = ?",
                    updates,
                )

        return {"from": source, "to": target, "updated": len(updates)}

    def answer_card(self, card_id: int, ease: int) -> dict[str, JSONValue]:
        if ease not in {1, 2, 3, 4}:
            raise ValueError("ease must be one of 1, 2, 3, 4")

        with self._connect_write() as conn:
            row = conn.execute(
                """
                SELECT
                    id, nid, did, ord, mod, usn, type, queue, due, ivl, factor, reps,
                    lapses, left, odue, odid, flags, data
                FROM cards
                WHERE id = ?
                """,
                (card_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"Card not found: {card_id}")

            review_dt = datetime.now(UTC)
            timing = self._timing(conn, int(review_dt.timestamp()))

            # A card in a filtered deck keeps its real schedule in odue and its
            # options come from the home deck. Anki (v3) answers it, sends it
            # home, then schedules normally; preview decks don't reschedule at
            # all, which this backend does not emulate.
            odid = int(row["odid"])
            home_did = odid if odid != 0 else int(row["did"])
            if odid != 0 and self._filtered_deck_reschedules(conn, int(row["did"])) is False:
                raise ValueError(
                    "Card is in a preview (non-rescheduling) filtered deck; "
                    "answer it in Anki or empty the deck first."
                )

            (
                scheduler,
                desired_retention,
                learn_count,
                relearn_count,
                params_source,
            ) = self._build_scheduler_ex(conn, home_did)

            fsrs_card = self._card_row_to_fsrs(
                row,
                timing=timing,
                now_dt=review_dt,
                learn_step_count=learn_count,
                relearn_step_count=relearn_count,
            )
            if fsrs_card.last_review is None:
                fsrs_card.last_review = self._last_review_time(conn, int(row["id"]))

            needs_seed = fsrs_card.state in (State.Review, State.Relearning) and (
                fsrs_card.stability is None
                or fsrs_card.difficulty is None
                or fsrs_card.last_review is None
            )
            if needs_seed:
                seeded = self._seed_fsrs_card_from_revlog(
                    conn,
                    scheduler,
                    card_id=int(row["id"]),
                    now_dt=review_dt
                )
                if seeded is not None:
                    fsrs_card.stability = seeded.stability
                    fsrs_card.difficulty = seeded.difficulty
                    fsrs_card.last_review = seeded.last_review
                else:
                    # Fallback for imported/legacy cards with no usable revlog.
                    ivl_days = int(row["ivl"] or 0)
                    factor = int(row["factor"] or 0)

                    fsrs_card.stability = float(max(1, ivl_days))

                    if factor > 0:
                        ease_mult = max(1.3, min(3.0, factor / 1000.0))
                        scaled = (ease_mult - 1.3) / (3.0 - 1.3)  # 0..1
                        fsrs_card.difficulty = max(
                            1.0,
                            min(10.0, 10.0 - (scaled * 9.0)),
                        )
                    else:
                        fsrs_card.difficulty = 5.0

                    mod_sec = int(row["mod"] or int(review_dt.timestamp()))
                    try:
                        fsrs_card.last_review = datetime.fromtimestamp(mod_sec, tz=UTC)
                    except (OSError, OverflowError, ValueError):
                        fsrs_card.last_review = review_dt

            if fsrs_card.state == State.Relearning and fsrs_card.step is None:
                fsrs_card.step = 0

            next_card = self._review_with_fuzz_seed(
                scheduler,
                fsrs_card,
                Rating(ease),
                review_datetime=review_dt,
                card_id=int(row["id"]),
                reps=int(row["reps"]),
            )

            (
                new_type,
                new_queue,
                new_due,
                new_ivl,
                new_left,
                next_due_epoch,
            ) = self._map_fsrs_result_to_anki(
                current_row=row,
                next_card=next_card,
                timing=timing,
                learn_step_count=learn_count,
                relearn_step_count=relearn_count,
                now_dt=review_dt,
            )

            now_sec = int(review_dt.timestamp())
            today_days = timing.days_elapsed
            reps = int(row["reps"]) + 1
            lapses = int(row["lapses"]) + (1 if ease == 1 else 0)
            raw_data = self._parse_card_data(str(row["data"] or ""))
            data_obj = dict(raw_data) if isinstance(raw_data, dict) else {}
            # rslib stores the original new-queue position when a card leaves New;
            # for a card on loan that is odue, not the filtered-deck slot.
            data_obj.setdefault(
                "pos", max(0, self._scheduling_due(row)) if int(row["type"]) == 0 else 0
            )
            # rslib CardData.last_review_time ("lrt", seconds); Anki prefers it over
            # the revlog when computing elapsed days.
            data_obj["lrt"] = now_sec
            data_obj["dr"] = round(desired_retention, 2)
            if next_card.stability is not None:
                data_obj["s"] = round(float(next_card.stability), 4)
            if next_card.difficulty is not None:
                data_obj["d"] = round(float(next_card.difficulty), 3)

            data_json = json.dumps(data_obj, separators=(",", ":"))

            conn.execute(
                f"""
                UPDATE cards
                SET
                    mod = ?,
                    usn = -1,
                    type = ?,
                    queue = ?,
                    due = ?,
                    ivl = ?,
                    reps = ?,
                    lapses = ?,
                    left = ?,
                    data = ?,
                    {LEAVE_FILTERED_DECK_SQL}
                WHERE id = ?
                """,
                (
                    now_sec,
                    new_type,
                    new_queue,
                    new_due,
                    new_ivl,
                    reps,
                    lapses,
                    new_left,
                    data_json,
                    card_id,
                ),
            )

            revlog_id = self._allocate_epoch_ms_id(conn, "revlog")
            old_due = self._scheduling_due(row)
            old_type = int(row["type"])
            old_queue = int(row["queue"])
            old_ivl = int(row["ivl"])

            # rslib as_revlog_interval: review/day-learn intervals are logged
            # in positive days, intraday learning in negative seconds.
            if new_queue == 2:
                logged_ivl = new_ivl
            elif new_queue == 3:
                logged_ivl = max(1, int(new_due - today_days))
            else:
                logged_ivl = -max(1, int(next_due_epoch - now_sec))
            if old_queue == 1:
                logged_last_ivl = -max(1, int(old_due - now_sec))
            elif old_queue == 3:
                logged_last_ivl = max(1, int(old_due - today_days))
            elif old_queue == 2:
                logged_last_ivl = max(1, old_ivl)
            else:
                logged_last_ivl = old_ivl

            if next_card.difficulty is None:
                logged_factor = int(row["factor"])
            else:
                logged_factor = max(100, min(1100, round(float(next_card.difficulty) * 100)))

            # rslib RevlogReviewKind comes from the card's state *before* the
            # answer: new/learning -> Learning, relearning -> Relearning, review ->
            # Review, or Filtered when a review is answered ahead of its due day.
            if old_type == 3:
                review_type = REVLOG_KIND_RELEARNING
            elif old_type == 2:
                review_type = (
                    REVLOG_KIND_FILTERED if old_due > today_days else REVLOG_KIND_REVIEW
                )
            else:
                review_type = REVLOG_KIND_LEARNING

            conn.execute(
                """
                INSERT INTO revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, type)
                VALUES (?, ?, -1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revlog_id,
                    card_id,
                    ease,
                    logged_ivl,
                    logged_last_ivl,
                    logged_factor,
                    0,
                    review_type,
                ),
            )

        return {
            "card_id": card_id,
            "ease": ease,
            "answered": True,
            "fsrs_params": params_source,
            "queue": new_queue,
            "type": new_type,
            "due": new_due,
            "interval": new_ivl,
            "revlog_id": revlog_id,
        }

    # ---- SQL query helpers ------------------------------------------------

    def get_revlog(self, card_id: int, limit: int = 50) -> list[dict[str, JSONValue]]:
        bounded_limit = max(1, min(limit, 1000))

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, cid, usn, ease, ivl, lastIvl, factor, time, type
                FROM revlog
                WHERE cid = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (card_id, bounded_limit),
            ).fetchall()

        return [self._revlog_row_to_item(row) for row in rows]

    # ---- low-level helpers ------------------------------------------------

    @staticmethod
    def _scheduling_due(row: sqlite3.Row) -> int:
        """The card's real due: ``odue`` while parked in a filtered deck, else ``due``."""
        keys = row.keys()
        if "odid" in keys and "odue" in keys and int(row["odid"]) != 0 and int(row["odue"]) > 0:
            return int(row["odue"])
        return int(row["due"])

    def _filtered_deck_reschedules(self, conn: sqlite3.Connection, did: int) -> bool | None:
        """``None`` if ``did`` is not a filtered deck, else its ``reschedule`` flag."""
        row = conn.execute("SELECT kind FROM decks WHERE id = ?", (did,)).fetchone()
        if row is None:
            return None
        kind = self._decode_deck_kind(bytes(row["kind"] or b""), did=did)
        kind_name, kind_msg = betterproto.which_one_of(kind, "kind")
        if kind_name != "filtered" or kind_msg is None:
            return None
        return bool(kind_msg.reschedule)

    def _read_config_json(self, conn: sqlite3.Connection, key: str) -> JSONValue:
        """Value of a key in Anki's ``config`` table (JSON blob), or None if absent."""
        try:
            row = conn.execute("SELECT val FROM config WHERE key = ?", (key,)).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            return None  # stripped-down fixture without a config table
        if row is None:
            return None
        raw = row["val"]
        text = raw.decode("utf-8") if isinstance(raw, bytes | bytearray) else str(raw)
        try:
            return cast(JSONValue, json.loads(text))
        except json.JSONDecodeError:
            return None

    def _timing(self, conn: sqlite3.Connection, now_sec: int) -> SchedTiming:
        """Anki's "today" for this collection (rslib ``timing_for_timestamp``).

        Reads ``crt`` plus the ``schedVer`` / ``rollover`` / ``creationOffset``
        config keys and dispatches exactly like rslib: no ``schedVer`` -> v1
        (plain 86400-second days), no ``creationOffset`` -> v2 legacy cutoff,
        both -> v2 new timezone handling. The current UTC offset is the local
        machine's, as Anki desktop uses.
        """
        row = conn.execute("SELECT crt FROM col LIMIT 1").fetchone()
        crt = int(row["crt"]) if row is not None else now_sec

        sched_ver = self._coerce_int_value(self._read_config_json(conn, "schedVer"))
        rollover: int | None = None
        if sched_ver is not None and sched_ver >= 2:
            configured = self._coerce_int_value(self._read_config_json(conn, "rollover"))
            rollover = configured if configured is not None else DEFAULT_ROLLOVER_HOUR
        creation_west = self._coerce_int_value(self._read_config_json(conn, "creationOffset"))

        return sched_timing_today(
            crt=crt,
            now=now_sec,
            creation_minutes_west=creation_west,
            current_minutes_west=local_minutes_west_for_stamp(now_sec),
            rollover_hour=rollover,
        )

    def _today_due_index(self, now_sec: int) -> int:
        # Only the timing tests call this now; production goes through
        # _search_context() / _timing(). Kept as the small public-ish probe.
        with self._connect() as conn:
            return self._timing(conn, now_sec).days_elapsed

    def _search_context(self) -> SearchContext:
        now_sec = int(time.time())
        with self._connect() as conn:
            timing = self._timing(conn, now_sec)
        return SearchContext(
            now_sec=now_sec,
            due_day_index=timing.days_elapsed,
            next_day_at=timing.next_day_at,
        )

    def _deck_filter(
        self,
        conn: sqlite3.Connection,
        deck: str | None,
    ) -> tuple[str, tuple[int, ...]]:
        if deck is None:
            return "", ()

        # The deck and its children, like Anki's deck list and ``deck:`` search:
        # ``--deck Japanese`` covers ``Japanese::Core`` too. Names are unique
        # case-insensitively in Anki, hence NOCASE rather than ``=``.
        name = deck.strip()
        rows = conn.execute(
            """
            SELECT id FROM decks
            WHERE name = ? COLLATE NOCASE
               OR name LIKE ? ESCAPE '\\'
            """,
            (name, f"{escape_like(name)}::%"),
        ).fetchall()
        ids = [int(row["id"]) for row in rows]
        if not ids:
            # impossible clause
            return " AND did IN (-1)", ()

        placeholders = ", ".join(["?"] * len(ids))
        return f" AND did IN ({placeholders})", tuple(ids)

    def _split_fields(self, value: str) -> list[str]:
        return value.split("\x1f") if value else []

    def _parse_tags(self, value: str) -> list[str]:
        stripped = value.strip()
        if not stripped:
            return []
        return [part for part in stripped.split(" ") if part]

    def _get_next_due_for_deck(self, deck_name: str) -> dict[str, JSONValue] | None:
        with self._connect() as conn:
            deck_row = conn.execute(
                "SELECT id FROM decks WHERE name = ?",
                (deck_name,),
            ).fetchone()
            if deck_row is None:
                return None
            did = int(deck_row["id"])
            timing = self._timing(conn, int(time.time()))
            # Only queue 1 stores an epoch; queues 2 and 3 store a day index.
            row = conn.execute(
                """
                SELECT queue, due
                FROM cards
                WHERE did = ? AND queue IN (1, 2, 3)
                ORDER BY CASE WHEN queue = 1 THEN due ELSE ? + due * 86400 END, id
                LIMIT 1
                """,
                (did, timing.day_start_epoch(0)),
            ).fetchone()
            if row is None:
                return None

        queue = int(row["queue"])
        due = int(row["due"])
        if queue == 1:
            return {"queue": queue, "epoch_secs": due}
        return {"queue": queue, "day_index": due, "epoch_secs": timing.day_start_epoch(due)}

    def _coerce_int_value(self, value: JSONValue) -> int | None:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                return None
        return None

    def _coerce_float_value(self, value: JSONValue) -> float | None:
        if isinstance(value, bool):
            return float(int(value))
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
        return None

    def _coerce_float_list(self, value: JSONValue) -> list[float]:
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(",") if part.strip()]
            return [float(part) for part in parts]
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            output: list[float] = []
            for item in value:
                parsed = self._coerce_float_value(cast(JSONValue, item))
                if parsed is None:
                    raise ValueError("Step values must be numeric.")
                output.append(parsed)
            return output
        raise ValueError("Step list must be comma-separated string or list of numbers.")

    def _deck_config_to_dict(self, cfg: DeckConfigConfig) -> dict[str, JSONValue]:
        return {
            "new_per_day": int(cfg.new_per_day),
            "reviews_per_day": int(cfg.reviews_per_day),
            "desired_retention": float(cfg.desired_retention),
            "maximum_review_interval": int(cfg.maximum_review_interval),
            "learn_steps": [float(step) for step in cfg.learn_steps],
            "relearn_steps": [float(step) for step in cfg.relearn_steps],
        }

    def _ensure_write_safe(self) -> None:
        from anki_cli.backends.detect import _anki_process_running, _sqlite_write_locked

        running = _anki_process_running()
        try:
            locked = _sqlite_write_locked(self.db_path)
        except sqlite3.Error as exc:
            # Fail closed, but as the typed refusal: an inconclusive probe is
            # "blocked", not a raw sqlite3 traceback out of the write path.
            raise DirectWriteBlockedError(
                f"Cannot verify the collection lock state at {self.db_path}: {exc}"
            ) from exc
        if running or locked:
            raise DirectWriteBlockedError(
                "Anki Desktop appears to be running while direct write was requested. "
                "Close Anki Desktop or use --backend ankiconnect."
            )

    def _allocate_row_id(self, conn: sqlite3.Connection, table: str) -> int:
        candidate = int(time.time() * 1000)
        while (
            conn.execute(f"SELECT 1 FROM {table} WHERE id = ? LIMIT 1", (candidate,)).fetchone()
            is not None
        ):
            candidate += 1
        return candidate

    def _allocate_epoch_ms_id(self, conn: sqlite3.Connection, table: str) -> int:
        candidate = int(time.time() * 1000)
        while (
            conn.execute(f"SELECT 1 FROM {table} WHERE id = ? LIMIT 1", (candidate,)).fetchone()
            is not None
        ):
            candidate += 1
        return candidate

    @staticmethod
    def _field_checksum(first_field: str) -> int:
        # Anki hashes the *text* of the first field, not its markup (rslib
        # ``field_checksum`` over ``strip_html_preserving_media_filenames``), so
        # CLI-written notes checksum identically to Anki-written ones.
        return AnkiDirectReadStore._checksum_of_stripped(
            _strip_html_preserving_media_filenames(first_field)
        )

    @staticmethod
    def _checksum_of_stripped(stripped_first_field: str) -> int:
        """rslib ``field_checksum``: first 32 bits of the SHA-1 of the stripped text."""
        digest = sha1(stripped_first_field.encode("utf-8")).hexdigest()
        return int(digest[:8], 16)

    @staticmethod
    def _sort_field_value(values: list[str], sort_idx: int) -> str:
        """``notes.sfld`` as Anki writes it (rslib ``Note::prepare_for_update``):
        the sort field with markup stripped, so the browser sorts ``<b>Zebra</b>``
        under Z and Check Database has nothing to rewrite. Falls back to the
        first field when the notetype's sort index is out of range."""
        if not values:
            return ""
        idx = sort_idx if 0 <= sort_idx < len(values) else 0
        return _strip_html_preserving_media_filenames(values[idx])

    def _find_duplicate_note_ids(
        self,
        conn: sqlite3.Connection,
        *,
        notetype_id: int,
        csum: int,
        first_stripped: str,
    ) -> list[int]:
        """Ids of existing notes Anki would flag as duplicates, ascending.

        Follows rslib's ``is_duplicate``: candidates are looked up by ``csum``
        scoped to the notetype (``csum`` alone collides across notetypes that
        share a front), then each hit is confirmed by comparing its stripped
        first field with ``first_stripped`` — the csum is only 32 bits. The
        caller has already rejected an empty first field.
        """
        rows = conn.execute(
            "SELECT id, flds FROM notes WHERE csum = ? AND mid = ? ORDER BY id",
            (csum, notetype_id),
        ).fetchall()
        matches: list[int] = []
        for row in rows:
            existing_first = self._split_fields(str(row["flds"] or ""))
            existing_stripped = _strip_html_preserving_media_filenames(
                existing_first[0] if existing_first else ""
            )
            if existing_stripped == first_stripped:
                matches.append(int(row["id"]))
        return matches

    def _coerce_tags(self, value: JSONValue) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            return [part for part in value.replace(",", " ").split(" ") if part.strip()]
        return []

    def _format_tags(self, tags: list[str]) -> str:
        normalized = sorted({tag.strip() for tag in tags if tag.strip()}, key=str.lower)
        if not normalized:
            return ""
        return f" {' '.join(normalized)} "

    def _build_guid(self, note_id: int) -> str:
        base = f"{note_id:x}"
        return f"ankicli-{base[-10:]}"

    def _insert_graves(self, conn: sqlite3.Connection, oids: list[int], grave_type: int) -> None:
        if not oids:
            return
        conn.executemany(
            "INSERT OR IGNORE INTO graves (oid, type, usn) VALUES (?, ?, -1)",
            [(oid, grave_type) for oid in oids],
        )

    def _resolve_deck_id(self, conn: sqlite3.Connection, deck_name: str) -> int:
        row = conn.execute(
            "SELECT id FROM decks WHERE name = ?",
            (deck_name.strip(),),
        ).fetchone()
        if row is None:
            raise LookupError(f"Deck not found: {deck_name}")
        return int(row["id"])

    def _load_notetype_schema(
        self,
        conn: sqlite3.Connection,
        notetype_name: str,
    ) -> tuple[int, list[str], int, bool]:
        row = conn.execute(
            "SELECT id, config FROM notetypes WHERE name = ?",
            (notetype_name.strip(),),
        ).fetchone()
        if row is None:
            raise LookupError(f"Notetype not found: {notetype_name}")

        mid = int(row["id"])
        config = self._decode_notetype_config(bytes(row["config"] or b""), ntid=mid)
        fields, sort_idx = self._field_schema_for_mid(conn, mid)
        is_cloze = int(config.kind) == 1
        return mid, fields, int(config.sort_field_idx or sort_idx), is_cloze

    def _field_schema_for_mid(
        self, conn: sqlite3.Connection, mid: int
    ) -> tuple[list[str], int]:
        field_rows = conn.execute(
            """
            SELECT ord, name
            FROM fields
            WHERE ntid = ?
            ORDER BY ord
            """,
            (mid,),
        ).fetchall()
        field_names = [str(row["name"]) for row in field_rows]
        nt_row = conn.execute(
            "SELECT config FROM notetypes WHERE id = ?",
            (mid,),
        ).fetchone()
        if nt_row is None:
            return field_names, 0
        config = self._decode_notetype_config(bytes(nt_row["config"] or b""), ntid=mid)
        return field_names, int(config.sort_field_idx)

    def _template_ords_for_note(
        self,
        conn: sqlite3.Connection,
        mid: int,
        field_values: list[str],
        is_cloze: bool,
        *,
        field_names: list[str] | None = None,
        has_tags: bool = False,
        ensure_not_empty: bool = True,
    ) -> list[int]:
        """Which cards a note should have (rslib ``CardGenContext::new_cards_required``).

        Normal notetypes: a template yields a card only if its front renders
        non-empty given the note's non-empty fields (plus Anki's special fields).
        A template that fails to parse never renders, as in Anki. With
        ``ensure_not_empty`` (new notes) the first template is used when nothing
        would render. The legacy ``reqs`` cache is not consulted, matching modern
        Anki.
        """
        if is_cloze:
            text = "\n".join(field_values)
            # rslib parses the cloze number as u16; anything larger is not a cloze.
            matches = {
                int(m.group(1)) for m in _CLOZE_RE.finditer(text) if int(m.group(1)) <= 0xFFFF
            }
            if not matches:
                return [0] if ensure_not_empty else []
            return sorted({min(499, max(0, idx - 1)) for idx in matches})

        rows = conn.execute(
            "SELECT ord, config FROM templates WHERE ntid = ? ORDER BY ord",
            (mid,),
        ).fetchall()
        if not rows:
            return [0] if ensure_not_empty else []

        names = field_names if field_names is not None else self._field_schema_for_mid(conn, mid)[0]
        nonempty = {
            name
            for name, value in zip(names, field_values, strict=False)
            if not field_is_empty(value)
        }
        for special in SPECIAL_FIELDS:
            if special in names or special == "FrontSide":
                continue
            if special == "Tags" and not has_tags:
                continue
            nonempty.add(special)

        ords: list[int] = []
        for row in rows:
            ord_ = int(row["ord"])
            cfg = self._decode_template_config(bytes(row["config"] or b""), ntid=mid, ord_=ord_)
            try:
                nodes = parse_template(cfg.q_format)
            except TemplateParseError:
                continue
            if template_renders_with_fields(nodes, nonempty):
                ords.append(ord_)
        if not ords and ensure_not_empty:
            return [int(rows[0]["ord"])]
        return ords

    def _remove_field_from_templates(
        self,
        conn: sqlite3.Connection,
        *,
        ntid: int,
        removed_name: str,
        first_remaining_field: str,
        is_cloze: bool,
        now_sec: int,
    ) -> int:
        """rslib ``update_templates_for_renamed_and_removed_fields`` (removal case)."""
        rows = conn.execute(
            "SELECT ord, config FROM templates WHERE ntid = ? ORDER BY ord", (ntid,)
        ).fetchall()
        changed = 0
        for row in rows:
            ord_ = int(row["ord"])
            tcfg = self._decode_template_config(bytes(row["config"] or b""), ntid=ntid, ord_=ord_)
            before = bytes(tcfg)
            for attr, question_side in (
                ("q_format", True),
                ("a_format", False),
                ("q_format_browser", True),
                ("a_format_browser", False),
            ):
                text = getattr(tcfg, attr, "") or ""
                if not text:
                    continue
                setattr(
                    tcfg,
                    attr,
                    remove_field_from_template(
                        text,
                        {removed_name},
                        first_remaining_field=first_remaining_field,
                        is_cloze=is_cloze,
                        question_side=question_side,
                    ),
                )
            after = bytes(tcfg)
            if after != before:
                conn.execute(
                    "UPDATE templates SET config = ?, mtime_secs = ?, usn = -1 "
                    "WHERE ntid = ? AND ord = ?",
                    (after, now_sec, ntid, ord_),
                )
                changed += 1
        return changed

    def _recompute_reqs(self, conn: sqlite3.Connection, ntid: int) -> None:
        """Rewrite the legacy ``reqs`` cache from the templates (rslib
        ``Notetype::updated_requirements``). Modern Anki recomputes this on every
        notetype save; older clients read it to decide which cards to make."""
        nt_row = conn.execute("SELECT config FROM notetypes WHERE id = ?", (ntid,)).fetchone()
        if nt_row is None:
            return
        config = self._decode_notetype_config(bytes(nt_row["config"] or b""), ntid=ntid)
        field_names, _ = self._field_schema_for_mid(conn, ntid)
        rows = conn.execute(
            "SELECT ord, config FROM templates WHERE ntid = ? ORDER BY ord", (ntid,)
        ).fetchall()

        kinds = {
            "any": NotetypeConfigCardRequirementKind.KIND_ANY,
            "all": NotetypeConfigCardRequirementKind.KIND_ALL,
            "none": NotetypeConfigCardRequirementKind.KIND_NONE,
        }
        reqs: list[NotetypeConfigCardRequirement] = []
        for row in rows:
            ord_ = int(row["ord"])
            tcfg = self._decode_template_config(bytes(row["config"] or b""), ntid=ntid, ord_=ord_)
            try:
                kind, ords = template_requirements(parse_template(tcfg.q_format), field_names)
            except TemplateParseError:
                kind, ords = "none", []
            reqs.append(
                NotetypeConfigCardRequirement(card_ord=ord_, kind=kinds[kind], field_ords=ords)
            )
        config.reqs = reqs
        conn.execute("UPDATE notetypes SET config = ? WHERE id = ?", (bytes(config), ntid))

    def _next_new_due(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(due), 0) AS max_due FROM cards WHERE queue = 0"
        ).fetchone()
        return int(row["max_due"] or 0) + 1

    def _set_cards_suspended(
        self,
        card_ids: list[int],
        *,
        suspended: bool,
    ) -> dict[str, JSONValue]:
        normalized_ids = sorted({int(cid) for cid in card_ids if int(cid) > 0})
        if not normalized_ids:
            return {"updated": 0, "card_ids": []}

        with self._connect_write() as conn:
            placeholders = ", ".join(["?"] * len(normalized_ids))
            existing_rows = conn.execute(
                f"SELECT id FROM cards WHERE id IN ({placeholders})",
                tuple(normalized_ids),
            ).fetchall()
            existing_ids = [int(row["id"]) for row in existing_rows]
            if not existing_ids:
                return {"updated": 0, "card_ids": []}

            existing_placeholders = ", ".join(["?"] * len(existing_ids))
            now_sec = int(time.time())
            if suspended:
                conn.execute(
                    (
                        "UPDATE cards SET queue = -1, mod = ?, usn = -1 "
                        f"WHERE id IN ({existing_placeholders})"
                    ),
                    (now_sec, *existing_ids),
                )
                return {
                    "updated": len(existing_ids),
                    "suspended": len(existing_ids),
                    "card_ids": existing_ids,
                }

            conn.execute(
                f"""
                UPDATE cards
                SET
                    queue = {queue_from_type_sql()},
                    mod = ?,
                    usn = -1
                WHERE id IN ({existing_placeholders})
                """,
                (now_sec, *existing_ids),
            )
            return {
                "updated": len(existing_ids),
                "unsuspended": len(existing_ids),
                "card_ids": existing_ids,
            }

    def _build_scheduler_ex(
        self,
        conn: sqlite3.Connection,
        deck_id: int,
    ) -> tuple[Scheduler, float, int, int, str]:
        """``_build_scheduler`` plus a label for which FSRS weights were used."""
        scheduler, retention, learn_n, relearn_n = self._build_scheduler(conn, deck_id)
        return scheduler, retention, learn_n, relearn_n, self._fsrs_params_source(conn, deck_id)

    def _fsrs_params_source(self, conn: sqlite3.Connection, deck_id: int) -> str:
        """Informational label for the result payload; never fails a review."""
        try:
            cfg, _retention = self._deck_config_for_deck(conn, deck_id)
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return "unknown"
            raise
        params, source = self._pick_fsrs_parameters(cfg)
        try:
            Scheduler(parameters=params)
        except ValueError:
            return "default"
        return source

    def _deck_config_for_deck(
        self, conn: sqlite3.Connection, deck_id: int
    ) -> tuple[DeckConfigConfig, float | None]:
        """The deck's options preset and its per-deck desired-retention override."""
        deck_row = conn.execute("SELECT kind FROM decks WHERE id = ?", (deck_id,)).fetchone()
        config_id = 1
        deck_retention: float | None = None
        if deck_row is not None:
            kind = self._decode_deck_kind(bytes(deck_row["kind"] or b""), did=deck_id)
            kind_name, kind_msg = betterproto.which_one_of(kind, "kind")
            if kind_name == "normal" and kind_msg is not None:
                config_id = int(kind_msg.config_id or 1)
                deck_retention = (
                    float(kind_msg.desired_retention)
                    if kind_msg.desired_retention is not None
                    else None
                )

        cfg_row = conn.execute(
            "SELECT config FROM deck_config WHERE id = ?",
            (config_id,),
        ).fetchone()
        cfg = (
            self._decode_deck_config(bytes(cfg_row["config"] or b""), dcid=config_id)
            if cfg_row is not None
            else DeckConfigConfig()
        )
        return cfg, deck_retention

    def _build_scheduler(
        self,
        conn: sqlite3.Connection,
        deck_id: int,
    ) -> tuple[Scheduler, float, int, int]:
        cfg, deck_retention = self._deck_config_for_deck(conn, deck_id)
        params, _params_source = self._pick_fsrs_parameters(cfg)
        desired_retention = (
            deck_retention
            if deck_retention is not None
            else float(cfg.desired_retention or 0.9)
        )
        learning_steps = self._to_timedeltas(
            cfg.learn_steps,
            default=[1.0, 10.0],
            assume_minutes=True,
        )
        relearning_steps = self._to_timedeltas(
            cfg.relearn_steps, default=[10.0], assume_minutes=True
        )
        max_interval = int(cfg.maximum_review_interval or 36500)

        try:
            scheduler = Scheduler(
                parameters=params,
                desired_retention=desired_retention,
                learning_steps=learning_steps,
                relearning_steps=relearning_steps,
                maximum_interval=max_interval,
            )
        except ValueError:
            # Upgraded legacy weights can land just outside py-fsrs's bounds.
            scheduler = Scheduler(
                parameters=list(FSRS6_DEFAULT_PARAMETERS),
                desired_retention=desired_retention,
                learning_steps=learning_steps,
                relearning_steps=relearning_steps,
                maximum_interval=max_interval,
            )
        return scheduler, desired_retention, len(learning_steps), len(relearning_steps)

    def _pick_fsrs_parameters(self, cfg: DeckConfigConfig) -> tuple[list[float], str]:
        """Newest non-empty weight set on the deck config, upgraded to FSRS-6."""
        for candidate in (cfg.fsrs_params_6, cfg.fsrs_params_5, cfg.fsrs_params_4):
            values = [float(item) for item in candidate]
            if values:
                params, source = upgrade_fsrs_parameters(values)
                if source == "default":
                    return params, source
                params, changed = clamp_fsrs_parameters(params)
                return params, f"{source}-clamped" if changed else source
        return list(FSRS6_DEFAULT_PARAMETERS), "default"

    @staticmethod
    def _review_with_fuzz_seed(
        scheduler: Scheduler,
        card: FSRSCard,
        rating: Rating,
        *,
        review_datetime: datetime,
        card_id: int,
        reps: int,
    ) -> FSRSCard:
        """py-fsrs draws its interval fuzz from the module-level ``random``;
        seed it per card + rep count like rslib so preview and answer agree."""
        state = random.getstate()
        try:
            random.seed(fsrs_fuzz_seed(card_id, reps))
            next_card, _log = scheduler.review_card(card, rating, review_datetime=review_datetime)
        finally:
            random.setstate(state)
        return next_card

    @staticmethod
    def _last_review_time(conn: sqlite3.Connection, card_id: int) -> datetime | None:
        """Most recent real review of the card, from the revlog (Anki's source of
        truth); manual reschedules (type 4/5) don't count."""
        row = conn.execute(
            """
            SELECT id FROM revlog
            WHERE cid = ? AND ease IN (1, 2, 3, 4) AND type IN (0, 1, 2, 3)
            ORDER BY id DESC LIMIT 1
            """,
            (card_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            return datetime.fromtimestamp(int(row["id"]) / 1000.0, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None

    def _to_timedeltas(
        self,
        values: list[float],
        *,
        default: list[float],
        assume_minutes: bool,
    ) -> list[timedelta]:
        source = values or default
        out: list[timedelta] = []
        for value in source:
            raw = float(value)
            if raw <= 0:
                continue
            seconds = raw * 60.0 if assume_minutes else raw
            out.append(timedelta(seconds=max(1, round(seconds))))
        return out if out else [timedelta(seconds=60)]

    def _card_row_to_fsrs(
        self,
        row: sqlite3.Row,
        *,
        timing: SchedTiming,
        now_dt: datetime,
        learn_step_count: int = 0,
        relearn_step_count: int = 0,
    ) -> FSRSCard:
        raw_data = self._parse_card_data(str(row["data"] or ""))
        data: dict[str, JSONValue] = (
            {str(key): cast(JSONValue, value) for key, value in raw_data.items()}
            if isinstance(raw_data, dict)
            else {}
        )

        stability = self._coerce_float_value(data.get("s"))
        difficulty = self._coerce_float_value(data.get("d"))

        # rslib CardData.last_review_time ("lrt"); callers fall back to the
        # revlog when it is absent (cards last answered by an older Anki).
        last_review: datetime | None = None
        lrt_value = self._coerce_int_value(data.get("lrt"))
        if lrt_value is not None:
            try:
                last_review = datetime.fromtimestamp(lrt_value, tz=UTC)
            except (TypeError, ValueError, OSError, OverflowError):
                last_review = None

        card_type = int(row["type"])
        queue = int(row["queue"])
        due_raw = self._scheduling_due(row)
        if card_type == 2:
            due_dt = datetime.fromtimestamp(timing.day_start_epoch(due_raw), tz=UTC)
            state = State.Review
        elif queue in (1, 3) or card_type in (1, 3):
            if is_intraday_learn_due(due_raw):
                due_dt = datetime.fromtimestamp(due_raw, tz=UTC)
            else:
                # Day-learn: due is a scheduling-day index.
                due_dt = datetime.fromtimestamp(timing.day_start_epoch(due_raw), tz=UTC)
            # Relearning is a property of type (3), not of the day-learn queue.
            state = State.Relearning if card_type == 3 else State.Learning
        else:
            due_dt = now_dt
            state = State.Learning

        # Anki packs left = today_remaining * 1000 + remaining_steps; the FSRS
        # step index is how many of the deck's steps are already behind us.
        left_raw = int(row["left"])
        step: int | None
        if left_raw > 0 and state in (State.Learning, State.Relearning):
            remaining = left_raw % 1000
            total = relearn_step_count if state == State.Relearning else learn_step_count
            step = max(0, total - remaining) if total > 0 else 0
        else:
            step = None

        return FSRSCard(
            card_id=int(row["id"]),
            state=state,
            step=step,
            stability=stability,
            difficulty=difficulty,
            due=due_dt,
            last_review=last_review,
        )

    def _map_fsrs_result_to_anki(
        self,
        *,
        current_row: sqlite3.Row,
        next_card: FSRSCard,
        timing: SchedTiming,
        learn_step_count: int,
        relearn_step_count: int,
        now_dt: datetime,
    ) -> tuple[int, int, int, int, int, int]:
        """Translate an FSRS result into Anki's ``(type, queue, due, ivl, left, due_epoch)``.

        ``now_dt`` must be the same instant handed to the FSRS scheduler as
        ``review_datetime`` so interval arithmetic is exact.
        """
        next_due_dt = next_card.due if next_card.due is not None else now_dt
        next_due_epoch = int(next_due_dt.timestamp())
        now_epoch = int(now_dt.timestamp())
        today_days = timing.days_elapsed

        if next_card.state == State.Review:
            # rslib: interval in whole days from today, due = today + interval.
            ivl_days = max(1, round((next_due_dt - now_dt).total_seconds() / 86400.0))
            due_days = today_days + ivl_days
            return (2, 2, due_days, ivl_days, 0, next_due_epoch)

        # Anki keeps learning steps shorter than a day in the intraday queue
        # (epoch due) and moves longer steps to the day-learn queue, whose due
        # is today + round(step / 1 day) (rslib LearnState -> InDays).
        def learn_queue_and_due() -> tuple[int, int]:
            delta = next_due_epoch - now_epoch
            if delta >= 86400:
                return (3, today_days + max(1, round(delta / 86400.0)))
            return (1, next_due_epoch)

        if next_card.state == State.Relearning:
            total = max(1, relearn_step_count)
            step = int(next_card.step or 0)
            remaining = max(1, total - step)
            left = (remaining * 1000) + remaining
            queue, due = learn_queue_and_due()
            return (3, queue, due, 0, left, next_due_epoch)

        # Learning (new or ongoing)
        old_type = int(current_row["type"])
        new_type = 1 if old_type != 2 else old_type
        total = max(1, learn_step_count)
        step = int(next_card.step or 0)
        remaining = max(1, total - step)
        left = (remaining * 1000) + remaining
        queue, due = learn_queue_and_due()
        return (new_type, queue, due, 0, left, next_due_epoch)

    def _decode_message(self, message: Any, blob: bytes, *, context: str) -> Any:
        try:
            return message.parse(blob)
        except Exception as exc:
            raise ValueError(
                f"Failed to decode protobuf for {context} ({len(blob)} bytes)."
            ) from exc


    def _decode_notetype_config(self, blob: bytes, *, ntid: int) -> NotetypeConfig:
        return self._decode_message(
            NotetypeConfig(),
            blob,
            context=f"notetypes.config ntid={ntid}",
        )


    def _decode_field_config(self, blob: bytes, *, ntid: int, ord_: int) -> NotetypeFieldConfig:
        return self._decode_message(
            NotetypeFieldConfig(),
            blob,
            context=f"fields.config ntid={ntid} ord={ord_}",
        )


    def _decode_template_config(
        self, blob: bytes, *, ntid: int, ord_: int
    ) -> NotetypeTemplateConfig:
        return self._decode_message(
            NotetypeTemplateConfig(),
            blob,
            context=f"templates.config ntid={ntid} ord={ord_}",
        )


    def _decode_deck_common(self, blob: bytes, *, did: int) -> DeckCommon:
        return self._decode_message(
            DeckCommon(),
            blob,
            context=f"decks.common did={did}",
        )


    def _decode_deck_kind(self, blob: bytes, *, did: int) -> DeckKindContainer:
        return self._decode_message(
            DeckKindContainer(),
            blob,
            context=f"decks.kind did={did}",
        )


    def _decode_deck_config(self, blob: bytes, *, dcid: int) -> DeckConfigConfig:
        return self._decode_message(
            DeckConfigConfig(),
            blob,
            context=f"deck_config.config id={dcid}",
        )


    def _load_notetype_parts(
        self,
        conn: sqlite3.Connection,
    ) -> tuple[
        dict[int, list[dict[str, JSONValue]]],
        dict[int, list[dict[str, JSONValue]]],
    ]:
        fields_by_ntid: dict[int, list[dict[str, JSONValue]]] = {}
        templates_by_ntid: dict[int, list[dict[str, JSONValue]]] = {}

        field_rows = conn.execute(
            """
            SELECT ntid, ord, name, config
            FROM fields
            ORDER BY ntid, ord
            """
        ).fetchall()

        for row in field_rows:
            ntid = int(row["ntid"])
            ord_ = int(row["ord"])
            name = str(row["name"])
            cfg_blob = bytes(row["config"] or b"")
            cfg = self._decode_field_config(cfg_blob, ntid=ntid, ord_=ord_)

            fields_by_ntid.setdefault(ntid, []).append(
                {
                    "ord": ord_,
                    "name": name,
                    "font": cfg.font_name,
                    "size": int(cfg.font_size),
                    "rtl": bool(cfg.rtl),
                    "sticky": bool(cfg.sticky),
                    "plain_text": bool(cfg.plain_text),
                }
            )

        template_rows = conn.execute(
            """
            SELECT ntid, ord, name, config
            FROM templates
            ORDER BY ntid, ord
            """
        ).fetchall()

        for row in template_rows:
            ntid = int(row["ntid"])
            ord_ = int(row["ord"])
            name = str(row["name"])
            cfg_blob = bytes(row["config"] or b"")
            cfg = self._decode_template_config(cfg_blob, ntid=ntid, ord_=ord_)

            templates_by_ntid.setdefault(ntid, []).append(
                {
                    "ord": ord_,
                    "name": name,
                    "qfmt": cfg.q_format,
                    "afmt": cfg.a_format,
                    "qfmt_browser": cfg.q_format_browser,
                    "afmt_browser": cfg.a_format_browser,
                }
            )

        return fields_by_ntid, templates_by_ntid


    def _read_deck_config_map(
        self,
        conn: sqlite3.Connection,
    ) -> dict[int, dict[str, JSONValue]]:
        rows = conn.execute(
            """
            SELECT id, name, config
            FROM deck_config
            ORDER BY id
            """
        ).fetchall()

        out: dict[int, dict[str, JSONValue]] = {}
        for row in rows:
            dcid = int(row["id"])
            cfg = self._decode_deck_config(bytes(row["config"] or b""), dcid=dcid)
            out[dcid] = {
                "id": dcid,
                "name": str(row["name"]),
                "new_per_day": int(cfg.new_per_day),
                "reviews_per_day": int(cfg.reviews_per_day),
                "desired_retention": float(cfg.desired_retention),
            }
        return out

    def _decode_due(
        self,
        *,
        card_type: int,
        queue: int,
        due_raw: int,
        timing: SchedTiming | None,
    ) -> dict[str, JSONValue]:
        if card_type == 0:
            return {"kind": "new_position", "raw": due_raw, "position": due_raw}

        if card_type in (1, 3):
            if is_intraday_learn_due(due_raw):
                return {"kind": "learn_epoch_secs", "raw": due_raw, "epoch_secs": due_raw}
            # Day-learn (queue 3): a learning step of >= 1 day stores a day index.
            out_learn: dict[str, JSONValue] = {
                "kind": "learn_day_index",
                "raw": due_raw,
                "day_index": due_raw,
            }
            if timing is not None:
                out_learn["epoch_secs"] = timing.day_start_epoch(due_raw)
                out_learn["days_from_today"] = due_raw - timing.days_elapsed
            return out_learn

        if card_type == 2:
            out: dict[str, JSONValue] = {
                "kind": "review_day_index",
                "raw": due_raw,
                "day_index": due_raw,
            }
            if timing is not None:
                out["epoch_secs"] = timing.day_start_epoch(due_raw)
                # Relative day count for display; epoch_secs is the *start* of the
                # due scheduling day, so flooring (epoch - now) would be off by one.
                out["days_from_today"] = due_raw - timing.days_elapsed
            return out

        return {"kind": "raw", "raw": due_raw, "queue": queue, "type": card_type}


    def _decode_left(self, left_raw: int) -> dict[str, int]:
        if left_raw < 0:
            return {"raw": left_raw}
        return {
            "raw": left_raw,
            "today_remaining": left_raw // 1000,
            "until_graduation": left_raw % 1000,
        }


    def _parse_card_data(self, raw: str) -> JSONValue:
        stripped = raw.strip()
        if not stripped:
            return {}

        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return raw

        if isinstance(parsed, (dict, list, str, int, float, bool)) or parsed is None:
            return parsed
        return raw

    def _revlog_row_to_item(self, row: sqlite3.Row) -> dict[str, JSONValue]:
        review_id = int(row["id"])
        interval_raw = int(row["ivl"])
        last_interval_raw = int(row["lastIvl"])
        factor_raw = int(row["factor"])
        review_type = int(row["type"])

        return {
            "id": review_id,
            "card_id": int(row["cid"]),
            "usn": int(row["usn"]),
            "ease": int(row["ease"]),
            "review_type": review_type,
            "review_type_name": self._revlog_type_name(review_type),
            "duration_ms": int(row["time"]),
            "reviewed_at_epoch_ms": review_id,
            "reviewed_at_epoch_secs": review_id // 1000,
            "interval": self._decode_revlog_interval(interval_raw),
            "last_interval": self._decode_revlog_interval(last_interval_raw),
            "factor": factor_raw,
            "factor_info": self._decode_revlog_factor(factor_raw),
        }


    def _decode_revlog_interval(self, value: int) -> dict[str, JSONValue]:
        # Anki convention: negative => seconds, positive => days.
        if value < 0:
            seconds = abs(value)
            return {
                "raw": value,
                "unit": "seconds",
                "seconds": seconds,
                "days": None,
            }

        return {
            "raw": value,
            "unit": "days",
            "days": value,
            "seconds": None,
        }


    def _decode_revlog_factor(self, factor: int) -> dict[str, JSONValue]:
        # FSRS review log uses roughly 100..1100 as difficulty*100.
        if 100 <= factor <= 1100:
            return {
                "raw": factor,
                "model": "fsrs_difficulty",
                "difficulty": factor / 100.0,
                "ease_multiplier": None,
            }

        # SM-2 style ease factor permille (eg 2500 => 2.5).
        if factor > 0:
            return {
                "raw": factor,
                "model": "sm2_ease_permille",
                "difficulty": None,
                "ease_multiplier": factor / 1000.0,
            }

        return {
            "raw": factor,
            "model": "unknown",
            "difficulty": None,
            "ease_multiplier": None,
        }


    def _revlog_type_name(self, review_type: int) -> str:
        return {
            0: "learn",
            1: "review",
            2: "relearn",
            3: "filtered",
            4: "manual",
        }.get(review_type, "unknown")

    def _seed_fsrs_card_from_revlog(
        self,
        conn: sqlite3.Connection,
        scheduler: Scheduler,
        *,
        card_id: int,
        now_dt: datetime,
    ) -> FSRSCard | None:
        rows = conn.execute(
            """
            SELECT id, ease, time
            FROM revlog
            WHERE cid = ? AND ease IN (1, 2, 3, 4)
            ORDER BY id ASC
            """,
            (card_id,),
        ).fetchall()

        if not rows:
            return None

        logs: list[ReviewLog] = []
        for r in rows:
            rid_ms = int(r["id"])
            ease = int(r["ease"])
            try:
                reviewed_at = datetime.fromtimestamp(rid_ms / 1000.0, tz=UTC)
            except (OSError, OverflowError, ValueError):
                continue

            logs.append(
                ReviewLog(
                    card_id=card_id,
                    rating=Rating(ease),
                    review_datetime=reviewed_at,
                    review_duration=int(r["time"]) if r["time"] is not None else None,
                )
            )

        if not logs:
            return None

        base = FSRSCard(card_id=card_id, due=now_dt)
        seeded = scheduler.reschedule_card(card=base, review_logs=logs)

        if seeded.stability is None or seeded.difficulty is None or seeded.last_review is None:
            return None
        return seeded
