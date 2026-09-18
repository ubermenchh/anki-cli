"""Render a card's question/answer from its note and notetype — once (#29).

This logic used to exist four times (``cards.py``, ``review.py``,
``tui/review_app.py``, ``tui/repl.py``) with small drifts between copies. Every
consumer now goes through ``render_card``; the CLI/TUI-specific parts (what to
do on failure, whether to hide the answer, HTML stripping) stay with the caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from anki_cli.core.template import render_template


@dataclass(frozen=True, slots=True)
class RenderedCard:
    notetype: str
    ord: int
    question: str
    answer: str
    css: str

    def as_dict(self, *, reveal_answer: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "notetype": self.notetype,
            "ord": self.ord,
            "question": self.question,
            "answer": self.answer,
            "css": self.css,
        }
        if not reveal_answer:
            out.pop("answer")
        return out


@dataclass(frozen=True, slots=True)
class CardRender:
    """``render_card``'s result: the raw card plus either a render or the reason
    there is none. Callers that need an exception can ``raise_for_error()``."""

    card: Any
    rendered: RenderedCard | None
    error: str | None

    def raise_for_error(self) -> RenderedCard:
        if self.rendered is None:
            raise RuntimeError(self.error or "Card could not be rendered.")
        return self.rendered


def extract_note_id(card: Mapping[str, Any]) -> int | None:
    """The note id under whichever key this backend used (direct: ``note``;
    AnkiConnect: ``note`` too, older shapes ``nid``/``noteId``/``note_id``)."""
    for key in ("note", "nid", "noteId", "note_id"):
        value = card.get(key)
        if isinstance(value, int):
            return value
    return None


def extract_ord(card: Mapping[str, Any]) -> int:
    value = card.get("ord")
    return int(value) if isinstance(value, int) else 0


def pick_template(templates: Mapping[str, Any], ord_: int) -> tuple[str, Mapping[str, Any]] | None:
    """``(name, template)`` for card ordinal ``ord_``.

    Prefers a template whose own ``ord`` matches (the direct backend supplies
    one); falls back to insertion order (all AnkiConnect gives us); then the
    first template; ``None`` when the notetype has none.
    """
    items = list(templates.items())
    for name, tmpl in items:
        if isinstance(tmpl, Mapping) and isinstance(tmpl.get("ord"), int) and tmpl["ord"] == ord_:
            return str(name), cast(Mapping[str, Any], tmpl)
    if 0 <= ord_ < len(items):
        name, tmpl = items[ord_]
        return str(name), tmpl if isinstance(tmpl, Mapping) else {}
    if items:
        name, tmpl = items[0]
        return str(name), tmpl if isinstance(tmpl, Mapping) else {}
    return None


def resolve_notetype_name(backend: Any, card: Mapping[str, Any], note_id: int) -> str | None:
    """Direct puts ``notetype_name`` on the card; AnkiConnect puts ``modelName``
    on the note; as a last resort match the note's ``mid`` against the notetype
    list (the one place the old ``cards.py`` copy went further than the others)."""
    raw = card.get("notetype_name")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    note_obj = backend.get_note(note_id)
    if not isinstance(note_obj, Mapping):
        return None
    model = note_obj.get("modelName")
    if isinstance(model, str) and model.strip():
        return model.strip()
    mid = note_obj.get("mid")
    if isinstance(mid, int):
        for nt in backend.get_notetypes():
            if isinstance(nt, Mapping) and nt.get("id") == mid:
                name = nt.get("name")
                return name.strip() if isinstance(name, str) else None
    return None


def render_card(backend: Any, card_id: int) -> CardRender:
    """Fetch ``card_id`` and render its front and back."""
    card_obj = backend.get_card(card_id)
    card_map = cast(Mapping[str, Any], card_obj) if isinstance(card_obj, Mapping) else {}

    note_id = extract_note_id(card_map)
    ord_ = extract_ord(card_map)
    if note_id is None:
        return CardRender(card_obj, None, "Card has no note id.")

    fields_map = backend.get_note_fields(note_id=note_id, fields=None)
    notetype_name = resolve_notetype_name(backend, card_map, note_id)
    if not notetype_name:
        return CardRender(card_obj, None, "Unable to determine notetype.")

    nt_detail = backend.get_notetype(notetype_name)
    kind = str(nt_detail.get("kind", "normal")).lower()
    templates_raw = nt_detail.get("templates")
    templates = cast(Mapping[str, Any], templates_raw) if isinstance(templates_raw, Mapping) else {}
    picked = pick_template(templates, ord_)
    if picked is None:
        return CardRender(card_obj, None, f"No templates found for notetype '{notetype_name}'.")

    _, tpl = picked
    front_tmpl = str(tpl.get("Front") or "")
    back_tmpl = str(tpl.get("Back") or "")

    if kind == "cloze":
        cloze_index = ord_ + 1
        question = render_template(
            front_tmpl, fields_map, cloze_index=cloze_index, reveal_cloze=False
        )
        answer = render_template(
            back_tmpl,
            fields_map,
            front_side=question,
            cloze_index=cloze_index,
            reveal_cloze=True,
        )
    else:
        question = render_template(front_tmpl, fields_map)
        answer = render_template(back_tmpl, fields_map, front_side=question)

    css = ""
    styling = nt_detail.get("styling")
    if isinstance(styling, Mapping):
        css = str(cast(Mapping[str, Any], styling).get("css") or "")

    return CardRender(
        card_obj,
        RenderedCard(notetype=notetype_name, ord=ord_, question=question, answer=answer, css=css),
        None,
    )
