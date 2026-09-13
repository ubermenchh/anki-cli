from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as _dc_field

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


# ---------------------------------------------------------------------------
# Card generation support (port of rslib/src/template.rs: template_is_empty,
# renders_with_fields, requirements; and template::field_is_empty).
# ---------------------------------------------------------------------------

# Fields Anki injects at render time; they count as non-empty for card
# generation (except FrontSide, and Tags only when the note has tags).
SPECIAL_FIELDS: frozenset[str] = frozenset(
    {"FrontSide", "Card", "CardFlag", "Deck", "Subdeck", "Tags", "Type", "CardID"}
)

# rslib template::field_is_empty: only whitespace and/or empty BR/DIV tags.
_FIELD_EMPTY_RE = re.compile(r"^(?:\s|</?(?:br|div) ?/?>)*$", re.IGNORECASE | re.DOTALL)
_HANDLEBAR_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
# rslib strips HTML comment delimiters that directly wrap a directive, so
# `<!--{{^a}}-->` behaves like `{{^a}}`.
_COMMENTED_DIRECTIVE_RE = re.compile(r"<!--\s*(\{\{.*?\}\})\s*-->", re.DOTALL)


def field_is_empty(text: str) -> bool:
    return _FIELD_EMPTY_RE.match(text or "") is not None


@dataclass
class TemplateNode:
    kind: str  # "text" | "replacement" | "conditional" | "negated"
    key: str = ""
    children: list[TemplateNode] = _dc_field(default_factory=list)


def parse_template(text: str) -> list[TemplateNode]:
    """Minimal handlebars parse: replacements (filters stripped), {{#x}}, {{^x}}."""
    text = _COMMENTED_DIRECTIVE_RE.sub(r"\1", text or "")
    root: list[TemplateNode] = []
    stack: list[tuple[str, list[TemplateNode]]] = [("", root)]
    pos = 0
    for match in _HANDLEBAR_RE.finditer(text):
        if match.start() > pos:
            stack[-1][1].append(TemplateNode("text"))
        raw = match.group(1).strip()
        pos = match.end()
        if not raw:
            continue
        head, body = raw[0], raw[1:].strip()
        if head == "#":
            node = TemplateNode("conditional", key=body)
            stack[-1][1].append(node)
            stack.append((body, node.children))
        elif head == "^":
            node = TemplateNode("negated", key=body)
            stack[-1][1].append(node)
            stack.append((body, node.children))
        elif head == "/":
            # Pop to the matching open tag; tolerate mismatches like rslib's
            # lenient parser rather than failing generation outright.
            for depth in range(len(stack) - 1, 0, -1):
                if stack[depth][0] == body:
                    del stack[depth:]
                    break
        else:
            # rslib: key is the text after the last ':' (filters stripped).
            key = raw.rsplit(":", 1)[-1].strip()
            stack[-1][1].append(TemplateNode("replacement", key=key))
    return root


def _template_is_empty(
    nonempty: frozenset[str] | set[str], nodes: list[TemplateNode], check_negated: bool
) -> bool:
    for node in nodes:
        if node.kind == "replacement":
            if node.key in nonempty:
                return False
        elif node.kind == "conditional":
            if node.key not in nonempty:
                continue
            if not _template_is_empty(nonempty, node.children, check_negated):
                return False
        elif node.kind == "negated":
            if check_negated and node.key in nonempty:
                continue
            if not _template_is_empty(nonempty, node.children, check_negated):
                return False
    return True


def template_renders_with_fields(nodes: list[TemplateNode], nonempty: set[str]) -> bool:
    """True if the front would render non-empty given these non-empty field names.

    This is what Anki uses to decide whether to generate a card (rslib
    ``ParsedTemplate::renders_with_fields``); ``reqs`` is only a legacy cache.
    """
    return not _template_is_empty(nonempty, nodes, check_negated=True)


def template_requirements(
    nodes: list[TemplateNode], field_names: list[str]
) -> tuple[str, list[int]]:
    """Legacy ``reqs`` entry for one template (rslib ``ParsedTemplate::requirements``).

    Returns ``("any", ords)`` if any single listed field suffices, ``("all",
    ords)`` if a set of fields is jointly required, or ``("none", [])`` if the
    template can never render from note fields.
    """

    def renders(nonempty: set[str]) -> bool:
        return not _template_is_empty(nonempty, nodes, check_negated=False)

    any_ords = [ord_ for ord_, name in enumerate(field_names) if renders({name})]
    if any_ords:
        return "any", any_ords

    all_fields = set(field_names)
    required = set(range(len(field_names)))
    for ord_, name in enumerate(field_names):
        # can we remove this field and still render?
        if renders(all_fields - {name}):
            required.discard(ord_)
    if required and renders(all_fields):
        return "all", sorted(required)
    return "none", []
