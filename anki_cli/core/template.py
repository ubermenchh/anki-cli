from __future__ import annotations

import re

_FIELD_RE = re.compile(r"\{\{([A-Za-z0-9_ ]+)\}\}")
_SECTION_RE = re.compile(r"\{\{([#^])([A-Za-z0-9_ ]+)\}\}(.*?)\{\{/\2\}\}", re.DOTALL)
_CLOZE_RE = re.compile(r"\{\{c(\d+)::(.*?)(?:::(.*?))?\}\}", re.DOTALL)


def _field_value(fields: dict[str, str], name: str) -> str:
    return str(fields.get(name.strip(), ""))


def _render_sections(template: str, fields: dict[str, str]) -> str:
    text = template
    while True:
        changed = False

        def repl(match: re.Match[str]) -> str:
            nonlocal changed
            changed = True
            mode = match.group(1)
            key = match.group(2).strip()
            body = match.group(3)
            has_value = bool(_field_value(fields, key).strip())
            if mode == "#":
                return body if has_value else ""
            return "" if has_value else body

        text = _SECTION_RE.sub(repl, text)
        if not changed:
            return text


def _render_fields(template: str, fields: dict[str, str], front_side: str | None) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        if key == "FrontSide":
            return front_side or ""
        return _field_value(fields, key)

    return _FIELD_RE.sub(repl, template)


def _render_cloze_field(value: str, *, reveal: bool, cloze_index: int | None) -> str:
    def repl(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        answer = match.group(2)
        hint = match.group(3) or ""

        if cloze_index is not None and idx != cloze_index:
            return answer

        if reveal:
            if hint:
                return f"{answer} ({hint})"
            return answer

        if hint:
            return f"[{hint}]"
        return "[...]"

    return _CLOZE_RE.sub(repl, value)


def render_template(
    template: str,
    fields: dict[str, str],
    *,
    front_side: str | None = None,
    cloze_index: int | None = None,
    reveal_cloze: bool = False,
) -> str:
    text = _render_sections(template, fields)
    text = _render_fields(text, fields, front_side)

    def cloze_field_repl(match: re.Match[str]) -> str:
        field_name = match.group(1).strip()
        raw = _field_value(fields, field_name)
        return _render_cloze_field(raw, reveal=reveal_cloze, cloze_index=cloze_index)

    text = re.sub(r"\{\{cloze:([A-Za-z0-9_ ]+)\}\}", cloze_field_repl, text)
    return text.strip()