"""Anki search syntax -> SQL ``WHERE`` fragments for the direct backend.

The only consumer is the direct SQLite store; AnkiConnect takes the query
string verbatim. Lives under ``db`` for that reason (#31).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

type SQLParam = str | int | float

TokenKind = Literal["TERM", "LPAREN", "RPAREN", "OR", "AND", "NOT", "EOF"]

# Anki spells the notetype filter ``note:``; ``notetype:`` is kept as this CLI's
# original spelling. Both compile to the same node.
_FILTER_PREFIXES = {
    "deck", "notetype", "note", "tag", "is", "flag", "prop", "nid", "cid", "added",
}
_PREFIX_ALIASES = {"note": "notetype"}
_IS_VALUES = {"new", "learn", "review", "due", "suspended", "buried"}
_PROP_PATTERN = re.compile(r"^(ivl|due|reps|lapses)(<=|>=|=|<|>)(-?\d+)$", re.IGNORECASE)
_PROP_COLUMNS = {
    "ivl": "ivl",
    "due": "due",
    "reps": "reps",
    "lapses": "lapses",
}


class SearchParseError(ValueError):
    def __init__(self, message: str, *, query: str, position: int | None = None) -> None:
        self.query = query
        self.position = position
        suffix = f" at position {position}" if position is not None else ""
        super().__init__(f"{message}{suffix}")


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    value: str
    position: int
    # Index in ``value`` of the first ``:`` that was not backslash-escaped, i.e.
    # the prefix/value separator. ``None`` when the term has no such colon, so
    # ``a\:b`` is a plain-text search for ``a:b`` (Anki's escape as well).
    separator: int | None = None


@dataclass(frozen=True, slots=True)
class AndNode:
    children: tuple[SearchNode, ...]


@dataclass(frozen=True, slots=True)
class OrNode:
    children: tuple[SearchNode, ...]


@dataclass(frozen=True, slots=True)
class NotNode:
    child: SearchNode


@dataclass(frozen=True, slots=True)
class FilterNode:
    kind: str
    value: str
    operator: str = ""


type SearchNode = AndNode | OrNode | NotNode | FilterNode


@dataclass(frozen=True, slots=True)
class CompiledSQL:
    where: str
    params: tuple[SQLParam, ...]
    joins: tuple[str, ...] = ()


@dataclass(slots=True)
class _Clause:
    where: str
    params: list[SQLParam]
    needs_notes_join: bool = False


def tokenize(query: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    length = len(query)

    while i < length:
        ch = query[i]

        if ch.isspace():
            i += 1
            continue

        if ch == "(":
            tokens.append(Token(kind="LPAREN", value=ch, position=i))
            i += 1
            continue

        if ch == ")":
            tokens.append(Token(kind="RPAREN", value=ch, position=i))
            i += 1
            continue

        if ch == "-":
            tokens.append(Token(kind="NOT", value=ch, position=i))
            i += 1
            continue

        start = i
        term, i, separator = _read_term(query, i)
        if not term:
            continue

        upper = term.upper()
        if upper == "OR":
            kind: TokenKind = "OR"
        elif upper == "AND":
            kind = "AND"
        elif upper == "NOT":
            kind = "NOT"
        else:
            kind = "TERM"

        tokens.append(Token(kind=kind, value=term, position=start, separator=separator))

    tokens.append(Token(kind="EOF", value="", position=length))
    return tokens


def parse(query: str) -> SearchNode:
    parser = _Parser(tokens=tokenize(query), query=query)
    return parser.parse()


@dataclass(frozen=True, slots=True)
class SearchContext:
    """The collection's "today", as the time-relative filters need it.

    ``now_sec`` bounds intraday learning (``queue = 1`` holds an epoch);
    ``due_day_index`` bounds review / day-learn (``queue IN (2, 3)`` hold a day
    index relative to ``col.crt``); ``next_day_at`` is the epoch second the next
    scheduling day starts, which is what ``added:N`` counts back from (rslib
    ``write_added``), so ``added:1`` means "since the last rollover", not
    "the last 24 hours".
    """

    now_sec: int
    due_day_index: int
    next_day_at: int


def compile_card_query(query: str, *, ctx: SearchContext) -> CompiledSQL:
    return compile_card(parse(query), ctx=ctx)


def compile_note_query(query: str, *, ctx: SearchContext) -> CompiledSQL:
    return compile_note(parse(query), ctx=ctx)


def compile_card(node: SearchNode, *, ctx: SearchContext) -> CompiledSQL:
    clause = _compile_card_node(node, ctx=ctx)
    joins: list[str] = []

    if clause.needs_notes_join:
        joins.append("JOIN notes AS n ON n.id = c.nid")

    return CompiledSQL(where=clause.where, params=tuple(clause.params), joins=tuple(joins))


def compile_note(node: SearchNode, *, ctx: SearchContext) -> CompiledSQL:
    clause = _compile_note_node(node, ctx=ctx)
    return CompiledSQL(where=clause.where, params=tuple(clause.params), joins=())


class _Parser:
    def __init__(self, *, tokens: list[Token], query: str) -> None:
        self._tokens = tokens
        self._query = query
        self._index = 0

    def parse(self) -> SearchNode:
        if self._peek().kind == "EOF":
            return AndNode(children=())

        node = self._parse_or()

        if self._peek().kind != "EOF":
            token = self._peek()
            raise SearchParseError("Unexpected token", query=self._query, position=token.position)

        return node

    def _parse_or(self) -> SearchNode:
        left = self._parse_and()
        children = [left]

        while self._peek().kind == "OR":
            self._advance()
            children.append(self._parse_and())

        if len(children) == 1:
            return children[0]
        return OrNode(children=tuple(children))

    def _parse_and(self) -> SearchNode:
        left = self._parse_unary()
        children = [left]

        while True:
            token = self._peek()

            if token.kind == "AND":
                self._advance()
                children.append(self._parse_unary())
                continue

            if token.kind in {"TERM", "LPAREN", "NOT"}:
                # Implicit AND via adjacency.
                children.append(self._parse_unary())
                continue

            break

        if len(children) == 1:
            return children[0]
        return AndNode(children=tuple(children))

    def _parse_unary(self) -> SearchNode:
        token = self._peek()
        if token.kind == "NOT":
            self._advance()
            return NotNode(child=self._parse_unary())
        return self._parse_atom()

    def _parse_atom(self) -> SearchNode:
        token = self._peek()

        if token.kind == "LPAREN":
            self._advance()
            if self._peek().kind == "RPAREN":
                raise SearchParseError(
                    "Empty parentheses are not allowed",
                    query=self._query,
                    position=self._peek().position,
                )

            node = self._parse_or()
            closing = self._peek()
            if closing.kind != "RPAREN":
                raise SearchParseError(
                    "Missing closing ')'",
                    query=self._query,
                    position=closing.position,
                )

            self._advance()
            return node

        if token.kind == "TERM":
            term_token = self._advance()
            return _term_to_filter(term_token, query=self._query)

        raise SearchParseError(
            "Expected a term, NOT, or '('",
            query=self._query,
            position=token.position,
        )

    def _peek(self) -> Token:
        return self._tokens[self._index]

    def _advance(self) -> Token:
        token = self._tokens[self._index]
        self._index += 1
        return token


def _read_term(query: str, start: int) -> tuple[str, int, int | None]:
    """Read one term; return ``(text, end, separator)``.

    ``separator`` is the index in ``text`` of the first colon that was not
    backslash-escaped (quotes do not protect a colon, matching Anki, where
    ``"deck:My Deck"`` is still a deck search and only ``\\:`` is literal).
    """
    i = start
    length = len(query)
    out: list[str] = []
    out_len = 0
    separator: int | None = None

    while i < length:
        ch = query[i]

        if ch.isspace() or ch in "()":
            break

        if ch in {"'", '"'}:
            quoted, i, quoted_sep = _read_quoted(query, i)
            if separator is None and quoted_sep is not None:
                separator = out_len + quoted_sep
            out.append(quoted)
            out_len += len(quoted)
            continue

        if ch == "\\" and i + 1 < length:
            out.append(query[i + 1])
            out_len += 1
            i += 2
            continue

        if ch == ":" and separator is None:
            separator = out_len
        out.append(ch)
        out_len += 1
        i += 1

    return "".join(out), i, separator


def _read_quoted(query: str, start: int) -> tuple[str, int, int | None]:
    quote_char = query[start]
    i = start + 1
    length = len(query)
    out: list[str] = []
    separator: int | None = None

    while i < length:
        ch = query[i]

        if ch == "\\" and i + 1 < length:
            out.append(query[i + 1])
            i += 2
            continue

        if ch == quote_char:
            return "".join(out), i + 1, separator

        if ch == ":" and separator is None:
            separator = len(out)
        out.append(ch)
        i += 1

    raise SearchParseError("Unterminated quoted string", query=query, position=start)


def _term_to_filter(token: Token, *, query: str) -> FilterNode:
    term = token.value.strip()
    if not term:
        raise SearchParseError("Empty term is not allowed", query=query, position=token.position)

    # ``token.value`` is already unescaped, so ``token.separator`` (recorded by
    # the tokenizer) is the only way to tell ``deck:x`` from a literal ``a\\:b``.
    # ``term`` was stripped; the separator index is relative to the unstripped
    # value, so shift it by the leading whitespace that was removed.
    separator = token.separator
    if separator is not None:
        separator -= len(token.value) - len(token.value.lstrip())
    if separator is None or separator <= 0:
        # No prefix (or an empty one like ``:foo``): plain-text search.
        return FilterNode(kind="text", value=term)

    prefix, raw_value = term[:separator], term[separator + 1 :]
    key = _PREFIX_ALIASES.get(prefix.casefold(), prefix.casefold())

    if key not in _FILTER_PREFIXES:
        # Anki treats an unknown prefix as a field search (``front:dog``); this
        # CLI does not implement field search, and silently falling back to
        # full-text (the old behaviour) returned wrong results for real Anki
        # filters like ``card:``, ``rated:`` or ``mid:``. Refuse instead.
        supported = ", ".join(sorted(_FILTER_PREFIXES - set(_PREFIX_ALIASES)))
        raise SearchParseError(
            f"Unsupported filter '{prefix}:'. Supported: {supported}. "
            "To search for a literal colon, escape it as '\\:'.",
            query=query,
            position=token.position,
        )

    value = raw_value.strip()
    if not value:
        raise SearchParseError(
            f"Missing value for '{key}:' filter",
            query=query,
            position=token.position,
        )

    if key == "deck" and value.casefold() == "current":
        raise SearchParseError(
            "deck:current is not supported (the CLI has no current deck); name the deck",
            query=query,
            position=token.position,
        )

    if key in {"deck", "notetype", "tag"}:
        return FilterNode(kind=key, value=value)

    if key in {"nid", "cid", "added"}:
        parsed = _parse_int(value, query=query, position=token.position, label=key)
        if key == "added":
            # rslib parse_added: u32 (negatives are a parse error), then n.max(1).
            if parsed < 0:
                raise SearchParseError(
                    "added: must be a non-negative number of days",
                    query=query,
                    position=token.position,
                )
            parsed = max(parsed, 1)
        return FilterNode(kind=key, value=str(parsed))

    if key == "is":
        normalized = value.casefold()
        if normalized not in _IS_VALUES:
            allowed = ", ".join(sorted(_IS_VALUES))
            raise SearchParseError(
                f"Invalid is: filter '{value}'. Allowed: {allowed}",
                query=query,
                position=token.position,
            )
        return FilterNode(kind="is", value=normalized)

    if key == "flag":
        flag = _parse_int(value, query=query, position=token.position, label="flag")
        if not 0 <= flag <= 7:
            raise SearchParseError(
                "flag must be between 0 and 7",
                query=query,
                position=token.position,
            )
        return FilterNode(kind="flag", value=str(flag))

    if key == "prop":
        compact = value.replace(" ", "")
        match = _PROP_PATTERN.fullmatch(compact)
        if match is None:
            raise SearchParseError(
                "Invalid prop filter. Expected e.g. prop:ivl>10, prop:due<=30",
                query=query,
                position=token.position,
            )

        prop_name = match.group(1).casefold()
        operator = match.group(2)
        threshold = _parse_int(
            match.group(3),
            query=query,
            position=token.position,
            label=f"prop:{prop_name}",
        )
        return FilterNode(kind="prop", value=f"{prop_name}:{threshold}", operator=operator)

    # Should be unreachable due to key checks.
    return FilterNode(kind="text", value=term)


def _parse_int(raw: str, *, query: str, position: int, label: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        raise SearchParseError(
            f"Invalid integer for {label}: '{raw}'",
            query=query,
            position=position,
        ) from exc


def _compile_card_node(node: SearchNode, *, ctx: SearchContext) -> _Clause:
    if isinstance(node, AndNode):
        return _compile_boolean(
            node.children, "AND", lambda child: _compile_card_node(child, ctx=ctx)
        )

    if isinstance(node, OrNode):
        return _compile_boolean(
            node.children, "OR", lambda child: _compile_card_node(child, ctx=ctx)
        )

    if isinstance(node, NotNode):
        child = _compile_card_node(node.child, ctx=ctx)
        return _Clause(
            where=f"NOT ({child.where})",
            params=list(child.params),
            needs_notes_join=child.needs_notes_join,
        )

    return _compile_card_filter(node, ctx=ctx)


def _compile_note_node(node: SearchNode, *, ctx: SearchContext) -> _Clause:
    if isinstance(node, AndNode):
        return _compile_boolean(
            node.children, "AND", lambda child: _compile_note_node(child, ctx=ctx)
        )

    if isinstance(node, OrNode):
        return _compile_boolean(
            node.children, "OR", lambda child: _compile_note_node(child, ctx=ctx)
        )

    if isinstance(node, NotNode):
        child = _compile_note_node(node.child, ctx=ctx)
        return _Clause(where=f"NOT ({child.where})", params=list(child.params))

    return _compile_note_filter(node, ctx=ctx)


def _compile_boolean(
    children: tuple[SearchNode, ...],
    operator: Literal["AND", "OR"],
    compile_child: Callable[[SearchNode], _Clause],
) -> _Clause:
    if not children:
        return _Clause(where="1=1", params=[])

    if len(children) == 1:
        return compile_child(children[0])

    pieces: list[str] = []
    params: list[SQLParam] = []
    needs_notes_join = False

    for child in children:
        compiled = compile_child(child)
        pieces.append(f"({compiled.where})")
        params.extend(compiled.params)
        needs_notes_join = needs_notes_join or compiled.needs_notes_join

    return _Clause(
        where=f" {operator} ".join(pieces),
        params=params,
        needs_notes_join=needs_notes_join,
    )


def _compile_card_filter(node: FilterNode, *, ctx: SearchContext) -> _Clause:
    if node.kind == "text":
        if not node.value:
            return _Clause(where="1=1", params=[])
        return _Clause(
            where="n.flds LIKE ? ESCAPE '\\'",
            params=[f"%{escape_like(node.value)}%"],
            needs_notes_join=True,
        )

    if node.kind == "deck":
        deck_sql, deck_params = _deck_clause(node.value, alias="c")
        return _Clause(where=deck_sql, params=deck_params)

    if node.kind == "notetype":
        return _Clause(
            where="n.mid IN (SELECT id FROM notetypes WHERE name LIKE ? ESCAPE '\\')",
            params=[_glob_to_like(node.value)],
            needs_notes_join=True,
        )

    if node.kind == "tag":
        tag_sql, tag_params = _tag_clause(node.value)
        return _Clause(where=tag_sql, params=tag_params, needs_notes_join=True)

    if node.kind == "nid":
        return _Clause(where="c.nid = ?", params=[int(node.value)])

    if node.kind == "cid":
        return _Clause(where="c.id = ?", params=[int(node.value)])

    if node.kind == "added":
        return _Clause(where="c.id > ?", params=[_added_cutoff_ms(node.value, ctx=ctx)])

    if node.kind == "is":
        is_sql, is_params = _is_clause(node.value, ctx=ctx, alias="c")
        return _Clause(where=is_sql, params=is_params)

    if node.kind == "flag":
        return _Clause(where="(c.flags & 7) = ?", params=[int(node.value)])

    if node.kind == "prop":
        prop_sql, prop_params = _prop_clause(node, alias="c")
        return _Clause(where=prop_sql, params=prop_params)

    raise ValueError(f"Unsupported card filter kind: {node.kind}")


def _compile_note_filter(node: FilterNode, *, ctx: SearchContext) -> _Clause:
    if node.kind == "text":
        if not node.value:
            return _Clause(where="1=1", params=[])
        return _Clause(
            where="n.flds LIKE ? ESCAPE '\\'",
            params=[f"%{escape_like(node.value)}%"],
        )

    if node.kind == "nid":
        return _Clause(where="n.id = ?", params=[int(node.value)])

    if node.kind == "tag":
        tag_sql, tag_params = _tag_clause(node.value)
        return _Clause(where=tag_sql, params=tag_params)

    if node.kind == "notetype":
        return _Clause(
            where="n.mid IN (SELECT id FROM notetypes WHERE name LIKE ? ESCAPE '\\')",
            params=[_glob_to_like(node.value)],
        )

    if node.kind == "added":
        # Anki has no note-level searches; "added" is a property of the card id.
        return _Clause(
            where="EXISTS (SELECT 1 FROM cards AS c WHERE c.nid = n.id AND c.id > ?)",
            params=[_added_cutoff_ms(node.value, ctx=ctx)],
        )

    if node.kind == "deck":
        deck_sql, deck_params = _deck_clause(node.value, alias="c")
        return _Clause(
            where=f"EXISTS (SELECT 1 FROM cards AS c WHERE c.nid = n.id AND ({deck_sql}))",
            params=deck_params,
        )

    if node.kind == "cid":
        return _Clause(
            where="EXISTS (SELECT 1 FROM cards AS c WHERE c.nid = n.id AND c.id = ?)",
            params=[int(node.value)],
        )

    if node.kind == "is":
        is_sql, is_params = _is_clause(node.value, ctx=ctx, alias="c")
        return _Clause(
            where=f"EXISTS (SELECT 1 FROM cards AS c WHERE c.nid = n.id AND ({is_sql}))",
            params=is_params,
        )

    if node.kind == "flag":
        return _Clause(
            where="EXISTS (SELECT 1 FROM cards AS c WHERE c.nid = n.id AND (c.flags & 7) = ?)",
            params=[int(node.value)],
        )

    if node.kind == "prop":
        prop_sql, prop_params = _prop_clause(node, alias="c")
        return _Clause(
            where=f"EXISTS (SELECT 1 FROM cards AS c WHERE c.nid = n.id AND ({prop_sql}))",
            params=prop_params,
        )

    raise ValueError(f"Unsupported note filter kind: {node.kind}")


def _deck_clause(value: str, *, alias: str) -> tuple[str, list[SQLParam]]:
    """rslib ``write_deck``: the named deck *and its children*, by ``did`` or, for a
    card visiting a filtered deck, by its home deck ``odid``.

    ``deck:Lang`` matches ``Lang`` and ``Lang::Spanish`` but not ``Language``;
    ``*`` globs. rslib's special values: ``deck:*`` is every card,
    ``deck:filtered`` is every card currently in a filtered deck. ``deck:current``
    needs the collection's current deck, which this compiler does not know, so
    it is rejected at parse time. Name matching is case-insensitive for ASCII
    only (SQLite ``LIKE``); rslib folds Unicode.
    """
    if value == "*":
        return ("1=1", [])
    if value.casefold() == "filtered":
        return (f"{alias}.odid != 0", [])
    pattern = _glob_to_like(value)
    ids_subquery = (
        "SELECT id FROM decks "
        "WHERE name LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\'"
    )
    child_pattern = f"{pattern}::%"
    # ``odid != 0`` is only an early-out (no deck has id 0); the IN handles it.
    return (
        f"({alias}.did IN ({ids_subquery}) "
        f"OR ({alias}.odid != 0 AND {alias}.odid IN ({ids_subquery})))",
        [pattern, child_pattern, pattern, child_pattern],
    )


def _tag_clause(value: str) -> tuple[str, list[SQLParam]]:
    """rslib ``write_tag``: ``tag:foo`` matches ``foo`` and ``foo::child``;
    ``tag:none`` is the untagged note. Tags are stored as `` a b `` with a space
    on each side, which is what the surrounding ``%`` / `` `` anchors rely on."""
    if value.casefold() == "none":
        return ("TRIM(n.tags) = ''", [])
    if value == "*":
        # rslib: ``tag:*`` is every note, tagged or not.
        return ("1=1", [])
    if " " in value:
        # A tag cannot contain a space, so nothing can match (rslib: false).
        return ("1=0", [])
    # Deviation from rslib: its glob ``*`` is ``\S*`` and cannot span into the
    # next tag; SQL ``%`` can, so ``tag:a*d`` also matches `` ab cd ``.
    pattern = _glob_to_like(value)
    return (
        "(n.tags LIKE ? ESCAPE '\\' OR n.tags LIKE ? ESCAPE '\\')",
        [f"% {pattern} %", f"% {pattern}::%"],
    )


def _added_cutoff_ms(value: str, *, ctx: SearchContext) -> int:
    """rslib ``write_added``: ``added:N`` is "created since N scheduling days
    ago", counted back from the next rollover, compared against the card id
    (creation time in ms). ``added:1`` is today's cards."""
    days = int(value)
    return (ctx.next_day_at - days * 86_400) * 1_000


def _is_clause(
    value: str,
    *,
    ctx: SearchContext,
    alias: str,
) -> tuple[str, list[SQLParam]]:
    now_sec = ctx.now_sec
    due_day_index = ctx.due_day_index
    # rslib write_state: New / Review are card *types* (so a suspended new card
    # is still is:new, and a relearning card is still is:review); Learn,
    # Suspended, Buried and Due look at the queue.
    if value == "new":
        return (f"{alias}.type = 0", [])

    if value == "learn":
        return (f"{alias}.queue IN (1, 3)", [])

    if value == "review":
        return (f"{alias}.type IN (2, 3)", [])

    if value == "suspended":
        return (f"{alias}.queue = -1", [])

    if value == "buried":
        return (f"{alias}.queue IN (-2, -3)", [])

    if value == "due":
        # rslib StateKind::Due: learning or review cards whose due time has
        # passed. New cards (queue 0) are never "due" — Anki's is:due does not
        # include them, and the CLI used to, which made every "is:due" count
        # and pick include the whole new queue. queue 1 (intraday learn) stores
        # an epoch; queues 2 (review) and 3 (day-learn) a day index.
        # Queue 4 (preview repeat) holds an epoch like queue 1.
        return (
            "("
            f"({alias}.queue IN (1, 4) AND {alias}.due <= ?) OR "
            f"({alias}.queue IN (2, 3) AND {alias}.due <= ?)"
            ")",
            [now_sec, due_day_index],
        )

    raise ValueError(f"Unsupported is: value: {value}")


def _prop_clause(node: FilterNode, *, alias: str) -> tuple[str, list[SQLParam]]:
    if node.kind != "prop":
        raise ValueError("prop clause requested for non-prop node")

    if node.operator not in {"<", "<=", "=", ">=", ">"}:
        raise ValueError(f"Invalid operator: {node.operator}")

    prop_name, raw_threshold = node.value.split(":", 1)
    column_name = _PROP_COLUMNS[prop_name]
    threshold = int(raw_threshold)

    return (f"{alias}.{column_name} {node.operator} ?", [threshold])


def escape_like(value: str) -> str:
    """Escape ``value`` for a ``LIKE ? ESCAPE '\\'`` pattern (``\\``, ``%``, ``_``)."""
    escaped = value.replace("\\", "\\\\")
    escaped = escaped.replace("%", "\\%")
    escaped = escaped.replace("_", "\\_")
    return escaped


def _glob_to_like(value: str) -> str:
    escaped = escape_like(value)
    return escaped.replace("*", "%")
