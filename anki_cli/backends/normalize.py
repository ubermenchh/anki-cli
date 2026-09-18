"""Normalise raw AnkiConnect responses to the canonical shapes (#30).

The direct backend's key set (``models/entities.py``) is canonical. AnkiConnect
returns its own vocabulary — ``modelName`` for the notetype, ``noteId`` for a
note's id, ``fields`` as ``{name: {value, order}}`` — so an agent parsing
``--format json`` had to know which backend it was talking to.

Every function here is additive — it adds the canonical keys and leaves the
AnkiConnect ones in place (``modelName``, ``noteId``, ``question``, ``answer``,
``css``, ``nextReviews`` …) — with one exception: ``fields`` is *replaced*,
from ``{name: {value, order}}`` to the canonical list of values, with the names
alongside in ``field_names``. Pure functions over plain dicts; no HTTP.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from anki_cli.core.due import decode_due, decode_left
from anki_cli.models.output import JSONValue


def _int(value: Any, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _tags(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(t) for t in value if str(t).strip()]
    if isinstance(value, str):
        return [t for t in value.split() if t]
    return []


def ordered_fields(raw_fields: Any) -> tuple[list[str], list[str]]:
    """``(names, values)`` in notetype order from AnkiConnect's
    ``{name: {value, order}}`` (also accepts ``{name: value}``)."""
    if not isinstance(raw_fields, Mapping):
        return [], []
    entries: list[tuple[int, str, str]] = []
    for idx, (name, spec) in enumerate(cast(Mapping[str, Any], raw_fields).items()):
        if isinstance(spec, Mapping):
            spec_map = cast(Mapping[str, Any], spec)
            # AnkiConnect always sets ``order``; the insertion index is only a
            # fallback for hand-built dicts and would misorder a mixed input.
            order = _int(spec_map.get("order"), idx)
            raw_value = spec_map.get("value")
            value = "" if raw_value is None else str(raw_value)
        else:
            order, value = idx, str(spec)
        entries.append((order, str(name), value))
    entries.sort(key=lambda e: e[0])
    return [e[1] for e in entries], [e[2] for e in entries]


def normalize_card(raw: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
    """``cardsInfo`` row -> canonical ``Card`` keys, AnkiConnect keys retained.

    ``due_info`` is decoded from ``type``/``queue``/``due`` exactly as the direct
    backend does, minus ``epoch_secs`` for day-index kinds: converting a day
    index to a time needs the collection's rollover timing, which AnkiConnect
    does not expose.
    """
    out: dict[str, JSONValue] = dict(raw)
    card_type = _int(raw.get("type"))
    queue = _int(raw.get("queue"))
    due = _int(raw.get("due"))

    names, values = ordered_fields(raw.get("fields"))
    out["cardId"] = _int(raw.get("cardId"))
    out["note"] = _int(raw.get("note"))
    out["deckName"] = _str(raw.get("deckName"))
    out["notetype_name"] = _str(raw.get("modelName"))
    out["ord"] = _int(raw.get("ord"))
    out["type"] = card_type
    out["queue"] = queue
    out["due"] = due
    out["interval"] = _int(raw.get("interval"))
    out["factor"] = _int(raw.get("factor"))
    out["reps"] = _int(raw.get("reps"))
    out["lapses"] = _int(raw.get("lapses"))
    out["left"] = _int(raw.get("left"))
    out["fields"] = values
    out["field_names"] = names
    out["tags"] = _tags(raw.get("tags"))
    out["due_info"] = decode_due(card_type=card_type, queue=queue, due_raw=due, timing=None)
    out["left_info"] = cast(dict[str, JSONValue], decode_left(_int(raw.get("left"))))
    if "flags" in raw:
        out["flags"] = _int(raw.get("flags"))
    return out


def normalize_note(raw: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
    """``notesInfo`` row -> canonical ``Note`` keys; ``fields`` becomes an
    ordered list with ``field_names`` alongside. ``id`` is set from ``noteId``."""
    out: dict[str, JSONValue] = dict(raw)
    names, values = ordered_fields(raw.get("fields"))
    out["id"] = _int(raw.get("noteId", raw.get("id")))
    out["notetype_name"] = _str(raw.get("modelName"))
    out["mod"] = _int(raw.get("mod"))
    out["tags"] = _tags(raw.get("tags"))
    out["fields"] = values
    out["field_names"] = names
    return out


def normalize_deck(raw: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
    """AnkiConnect knows only ``id``/``name``; ``kind`` is reported as
    ``"unknown"`` rather than guessed."""
    out: dict[str, JSONValue] = dict(raw)
    out["id"] = _int(raw.get("id"))
    out["name"] = _str(raw.get("name"))
    out.setdefault("kind", "unknown")
    return out


def normalize_notetype(raw: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
    """Give AnkiConnect ``modelTemplates`` the direct backend's ``ord`` per
    template (insertion order, which is how AnkiConnect orders them) so
    ``core.render.pick_template`` no longer needs an index fallback."""
    out: dict[str, JSONValue] = dict(raw)
    templates_raw = raw.get("templates")
    templates: dict[str, JSONValue] = {}
    if isinstance(templates_raw, Mapping):
        for ord_, (name, tmpl) in enumerate(cast(Mapping[str, Any], templates_raw).items()):
            body: dict[str, JSONValue] = (
                dict(cast(Mapping[str, JSONValue], tmpl)) if isinstance(tmpl, Mapping) else {}
            )
            body.setdefault("ord", ord_)
            templates[str(name)] = body
    out["templates"] = templates
    styling = raw.get("styling")
    out["styling"] = (
        dict(cast(Mapping[str, JSONValue], styling)) if isinstance(styling, Mapping) else {}
    )
    out.setdefault("kind", "normal")
    return out


__all__ = [
    "normalize_card",
    "normalize_deck",
    "normalize_note",
    "normalize_notetype",
    "ordered_fields",
]
