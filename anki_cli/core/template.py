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
# Template structure (port of rslib/src/template.rs: tokens / parse_inner,
# template_is_empty, renders_with_fields, requirements, rename_and_remove_fields,
# template_to_string; and template::field_is_empty).
# ---------------------------------------------------------------------------

# Fields Anki injects at render time; they count as non-empty for card
# generation (except FrontSide, and Tags only when the note has tags).
SPECIAL_FIELDS: frozenset[str] = frozenset(
    {"FrontSide", "Card", "CardFlag", "Deck", "Subdeck", "Tags", "Type", "CardID"}
)

_COMMENT_START = "<!--"
_COMMENT_END = "-->"
_ALT_HANDLEBAR_DIRECTIVE = "{{=<% %>=}}"

# rslib template::field_is_empty: only whitespace and/or empty BR/DIV tags.
# [[:space:]] in the regex crate is ASCII-only, hence re.ASCII.
_FIELD_EMPTY_RE = re.compile(
    r"^(?:\s|</?(?:br|div) ?/?>)*$", re.IGNORECASE | re.DOTALL | re.ASCII
)


def field_is_empty(text: str) -> bool:
    return _FIELD_EMPTY_RE.match(text or "") is not None


class TemplateParseError(ValueError):
    """Unbalanced conditional sections (rslib ConditionalNotOpen / NotClosed)."""


@dataclass
class TemplateNode:
    kind: str  # "text" | "comment" | "replacement" | "conditional" | "negated"
    key: str = ""  # replacement/conditional field name; raw text for text/comment
    filters: list[str] = _dc_field(default_factory=list)  # replacement only, outermost first
    children: list[TemplateNode] = _dc_field(default_factory=list)


def _tokens(template: str):
    """Yield (kind, payload) like rslib ``tokens``: handlebar, comment, or text.

    At every position a handlebar is tried before a comment, so a directive
    inside an HTML comment is a comment, not a directive.
    """
    text = template
    start_tag, end_tag = "{{", "}}"
    if text.lstrip().startswith(_ALT_HANDLEBAR_DIRECTIVE):
        text = text.lstrip()[len(_ALT_HANDLEBAR_DIRECTIVE) :]
        start_tag, end_tag = "<%", "%>"

    pos = 0
    n = len(text)
    while pos < n:
        i = pos
        found = None
        while i < n:
            if text.startswith(start_tag, i):
                close = text.find(end_tag, i + len(start_tag))
                if close != -1:
                    found = (i, close + len(end_tag), "handle", text[i + len(start_tag) : close])
                    break
            if text.startswith(_COMMENT_START, i):
                close = text.find(_COMMENT_END, i + len(_COMMENT_START))
                if close != -1:
                    found = (
                        i,
                        close + len(_COMMENT_END),
                        "comment",
                        text[i + len(_COMMENT_START) : close],
                    )
                    break
            i += 1
        if found is None:
            yield "text", text[pos:]
            return
        tok_start, tok_end, kind, payload = found
        if tok_start > pos:
            yield "text", text[pos:tok_start]
        yield kind, payload
        pos = tok_end


def _classify_handle(raw: str) -> tuple[str, str]:
    """rslib ``classify_handle``: returns (node kind, key-with-filters)."""
    start = raw.lstrip("{").strip()
    if len(start) < 2:
        return "replacement", start
    if start.startswith("#"):
        return "conditional", start[1:].lstrip()
    if start.startswith("/"):
        return "close", start[1:].lstrip()
    if start.startswith("^"):
        return "negated", start[1:].lstrip()
    return "replacement", start


def parse_template(text: str) -> list[TemplateNode]:
    """Parse a card template into nodes; raises TemplateParseError like rslib."""
    root: list[TemplateNode] = []
    stack: list[tuple[str | None, list[TemplateNode]]] = [(None, root)]
    for kind, payload in _tokens(text or ""):
        target = stack[-1][1]
        if kind == "text":
            target.append(TemplateNode("text", key=payload))
            continue
        if kind == "comment":
            target.append(TemplateNode("comment", key=payload))
            continue
        node_kind, body = _classify_handle(payload)
        if node_kind == "replacement":
            # rslib: key is the text after the last ':' (not re-trimmed).
            parts = body.split(":")
            target.append(TemplateNode("replacement", key=parts[-1], filters=parts[:-1]))
        elif node_kind in ("conditional", "negated"):
            node = TemplateNode(node_kind, key=body)
            target.append(node)
            stack.append((body, node.children))
        else:  # close
            open_key = stack[-1][0]
            if open_key is None or open_key != body:
                raise TemplateParseError(
                    f"Closing tag {{{{/{body}}}}} does not match open tag "
                    f"{{{{#{open_key}}}}}" if open_key else f"{{{{/{body}}}}} has no open tag"
                )
            stack.pop()
    if len(stack) > 1:
        raise TemplateParseError(f"Conditional {{{{#{stack[-1][0]}}}}} was not closed")
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


def remove_fields(nodes: list[TemplateNode], removed: set[str]) -> list[TemplateNode]:
    """rslib ``rename_and_remove_fields`` for the removal case: drop replacements
    of the field; a section keyed on it is unwrapped, keeping its children."""
    out: list[TemplateNode] = []
    for node in nodes:
        if node.kind == "replacement":
            if node.key not in removed:
                out.append(node)
        elif node.kind in ("conditional", "negated"):
            children = remove_fields(node.children, removed)
            if node.key in removed:
                out.extend(children)
            else:
                out.append(TemplateNode(node.kind, key=node.key, children=children))
        else:
            out.append(node)
    return out


def contains_field_replacement(nodes: list[TemplateNode], *, cloze_only: bool = False) -> bool:
    for node in nodes:
        if node.kind == "replacement":
            if not cloze_only or "cloze" in node.filters:
                return True
        elif node.kind in ("conditional", "negated") and contains_field_replacement(
            node.children, cloze_only=cloze_only
        ):
            return True
    return False


def nodes_to_string(nodes: list[TemplateNode]) -> str:
    """rslib ``template_to_string``: text and comments verbatim, directives canonical."""
    out: list[str] = []
    for node in nodes:
        if node.kind == "text":
            out.append(node.key)
        elif node.kind == "comment":
            out.append(f"{_COMMENT_START}{node.key}{_COMMENT_END}")
        elif node.kind == "replacement":
            out.append("{{" + ":".join([*node.filters, node.key]) + "}}")
        elif node.kind == "conditional":
            out.append(f"{{{{#{node.key}}}}}{nodes_to_string(node.children)}{{{{/{node.key}}}}}")
        elif node.kind == "negated":
            out.append(f"{{{{^{node.key}}}}}{nodes_to_string(node.children)}{{{{/{node.key}}}}}")
    return "".join(out)


def remove_field_from_template(
    template: str,
    removed: set[str],
    *,
    first_remaining_field: str,
    is_cloze: bool,
    question_side: bool,
) -> str:
    """rslib ``update_templates_for_renamed_and_removed_fields`` for one side.

    Unparseable templates are returned unchanged (rslib ignores them too).
    """
    try:
        nodes = parse_template(template)
    except TemplateParseError:
        return template
    nodes = remove_fields(nodes, removed)
    needs_field = question_side and not contains_field_replacement(nodes)
    needs_cloze = is_cloze and not contains_field_replacement(nodes, cloze_only=True)
    if needs_field or needs_cloze:
        nodes.append(
            TemplateNode(
                "replacement",
                key=first_remaining_field,
                filters=["cloze"] if is_cloze else [],
            )
        )
    return nodes_to_string(nodes)
