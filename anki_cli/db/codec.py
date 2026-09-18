"""Pure encode/decode helpers: protobuf blobs, the ``\\x1f`` field separator,
Anki's tag string, checksums (rslib ``field_checksum``), ``cards.data`` JSON.

Nothing here touches the database; ``CodecMixin`` is the bottom of the store's
mixin stack and depends on no other mixin.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from hashlib import sha1
from html.entities import name2codepoint
from typing import Any, cast

from anki_cli.core.due import decode_due, decode_left
from anki_cli.db.timing import SchedTiming
from anki_cli.models.output import JSONValue
from anki_cli.proto.anki.deck_config import DeckConfigConfig
from anki_cli.proto.anki.decks import DeckCommon, DeckKindContainer
from anki_cli.proto.anki.notetypes import (
    NotetypeConfig,
    NotetypeFieldConfig,
    NotetypeTemplateConfig,
)

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


class CodecMixin:
    """Stateless conversions shared by every other mixin."""

    def _split_fields(self, value: str) -> list[str]:
        return value.split("\x1f") if value else []

    def _parse_tags(self, value: str) -> list[str]:
        stripped = value.strip()
        if not stripped:
            return []
        return [part for part in stripped.split(" ") if part]

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

    @staticmethod
    def _field_checksum(first_field: str) -> int:
        # Anki hashes the *text* of the first field, not its markup (rslib
        # ``field_checksum`` over ``strip_html_preserving_media_filenames``), so
        # CLI-written notes checksum identically to Anki-written ones.
        return CodecMixin._checksum_of_stripped(
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

    def _decode_due(
        self,
        *,
        card_type: int,
        queue: int,
        due_raw: int,
        timing: SchedTiming | None,
    ) -> dict[str, JSONValue]:
        return decode_due(card_type=card_type, queue=queue, due_raw=due_raw, timing=timing)

    def _decode_left(self, left_raw: int) -> dict[str, int]:
        return decode_left(left_raw)

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
