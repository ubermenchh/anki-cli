from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

type SQLParam = str | int | float

TokenKind = Literal["TERM", "LPAREN", "RPAREN", "OR", "AND", "NOT", "EOF"]

_FILTER_PREFIXES = {"deck", "notetype", "tag", "is", "flag", "prop", "nid", "cid", "added"}
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
    needs_decks_join: bool = False


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
        term, i = _read_term(query, i)
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

        tokens.append(Token(kind=kind, value=term, position=start))

    tokens.append(Token(kind="EOF", value="", position=length))
    return tokens


def parse(query: str) -> SearchNode:
    parser = _Parser(tokens=tokenize(query), query=query)
    return parser.parse()


def compile_card_query(query: str, *, now_sec: int, due_day_index: int) -> CompiledSQL:
    return compile_card(parse(query), now_sec=now_sec, due_day_index=due_day_index)


def compile_note_query(query: str, *, now_sec: int, due_day_index: int) -> CompiledSQL:
    return compile_note(parse(query), now_sec=now_sec, due_day_index=due_day_index)


def compile_card(node: SearchNode, *, now_sec: int, due_day_index: int) -> CompiledSQL:
    clause = _compile_card_node(node, now_sec=now_sec, due_day_index=due_day_index)
    joins: list[str] = []

    if clause.needs_notes_join:
        joins.append("JOIN notes AS n ON n.id = c.nid")
    if clause.needs_decks_join:
        joins.append("LEFT JOIN decks AS d ON d.id = c.did")

    return CompiledSQL(where=clause.where, params=tuple(clause.params), joins=tuple(joins))


def compile_note(node: SearchNode, *, now_sec: int, due_day_index: int) -> CompiledSQL:
    clause = _compile_note_node(node, now_sec=now_sec, due_day_index=due_day_index)
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


def _read_term(query: str, start: int) -> tuple[str, int]:
    i = start
    length = len(query)
    out: list[str] = []

    while i < length:
        ch = query[i]

        if ch.isspace() or ch in "()":
            break

        if ch in {"'", '"'}:
            quoted, i = _read_quoted(query, i)
            out.append(quoted)
            continue

        if ch == "\\" and i + 1 < length:
            out.append(query[i + 1])
            i += 2
            continue

        out.append(ch)
        i += 1

    return "".join(out), i


def _read_quoted(query: str, start: int) -> tuple[str, int]:
    quote_char = query[start]
    i = start + 1
    length = len(query)
    out: list[str] = []

    while i < length:
        ch = query[i]

        if ch == "\\" and i + 1 < length:
            out.append(query[i + 1])
            i += 2
            continue

        if ch == quote_char:
            return "".join(out), i + 1

        out.append(ch)
        i += 1

    raise SearchParseError("Unterminated quoted string", query=query, position=start)


def _term_to_filter(token: Token, *, query: str) -> FilterNode:
    term = token.value.strip()
    if not term:
        raise SearchParseError("Empty term is not allowed", query=query, position=token.position)

    if ":" not in term:
        return FilterNode(kind="text", value=term)

    prefix, raw_value = term.split(":", 1)
    key = prefix.casefold()

    if key not in _FILTER_PREFIXES:
        # Keep backward compatibility: unknown prefix behaves like plain text.
        return FilterNode(kind="text", value=term)

    value = raw_value.strip()
    if not value:
        raise SearchParseError(
            f"Missing value for '{key}:' filter",
            query=query,
            position=token.position,
        )

    if key in {"deck", "notetype", "tag"}:
        return FilterNode(kind=key, value=value)

    if key in {"nid", "cid", "added"}:
        parsed = _parse_int(value, query=query, position=token.position, label=key)
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


def _compile_card_node(node: SearchNode, *, now_sec: int, due_day_index: int) -> _Clause:
    if isinstance(node, AndNode):
        return _compile_boolean(
            node.children,
            "AND",
            lambda child: _compile_card_node(child, now_sec=now_sec, due_day_index=due_day_index),
        )

    if isinstance(node, OrNode):
        return _compile_boolean(
            node.children,
            "OR",
            lambda child: _compile_card_node(child, now_sec=now_sec, due_day_index=due_day_index),
        )

    if isinstance(node, NotNode):
        child = _compile_card_node(node.child, now_sec=now_sec, due_day_index=due_day_index)
        return _Clause(
            where=f"NOT ({child.where})",
            params=list(child.params),
            needs_notes_join=child.needs_notes_join,
            needs_decks_join=child.needs_decks_join,
        )

    return _compile_card_filter(node, now_sec=now_sec, due_day_index=due_day_index)


def _compile_note_node(node: SearchNode, *, now_sec: int, due_day_index: int) -> _Clause:
    if isinstance(node, AndNode):
        return _compile_boolean(
            node.children,
            "AND",
            lambda child: _compile_note_node(child, now_sec=now_sec, due_day_index=due_day_index),
        )

    if isinstance(node, OrNode):
        return _compile_boolean(
            node.children,
            "OR",
            lambda child: _compile_note_node(child, now_sec=now_sec, due_day_index=due_day_index),
        )

    if isinstance(node, NotNode):
        child = _compile_note_node(node.child, now_sec=now_sec, due_day_index=due_day_index)
        return _Clause(where=f"NOT ({child.where})", params=list(child.params))

    return _compile_note_filter(node, now_sec=now_sec, due_day_index=due_day_index)


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
    needs_decks_join = False

    for child in children:
        compiled = compile_child(child)
        pieces.append(f"({compiled.where})")
        params.extend(compiled.params)
        needs_notes_join = needs_notes_join or compiled.needs_notes_join
        needs_decks_join = needs_decks_join or compiled.needs_decks_join

    return _Clause(
        where=f" {operator} ".join(pieces),
        params=params,
        needs_notes_join=needs_notes_join,
        needs_decks_join=needs_decks_join,
    )


def _compile_card_filter(node: FilterNode, *, now_sec: int, due_day_index: int) -> _Clause:
    if node.kind == "text":
        if not node.value:
            return _Clause(where="1=1", params=[])
        return _Clause(
            where="n.flds LIKE ? ESCAPE '\\'",
            params=[f"%{_escape_like(node.value)}%"],
            needs_notes_join=True,
        )

    if node.kind == "deck":
        return _Clause(
            where="d.name LIKE ? ESCAPE '\\'",
            params=[_glob_to_like(node.value)],
            needs_decks_join=True,
        )

    if node.kind == "notetype":
        return _Clause(
            where="n.mid IN (SELECT id FROM notetypes WHERE name LIKE ? ESCAPE '\\')",
            params=[_glob_to_like(node.value)],
            needs_notes_join=True,
        )

    if node.kind == "tag":
        tag_pattern = _glob_to_like(node.value)
        return _Clause(
            where="n.tags LIKE ? ESCAPE '\\'",
            params=[f"% {tag_pattern} %"],
            needs_notes_join=True,
        )

    if node.kind == "nid":
        return _Clause(where="c.nid = ?", params=[int(node.value)])

    if node.kind == "cid":
        return _Clause(where="c.id = ?", params=[int(node.value)])

    if node.kind == "added":
        cutoff = now_sec - (int(node.value) * 86400)
        return _Clause(
            where="n.mod >= ?",
            params=[cutoff],
            needs_notes_join=True,
        )

    if node.kind == "is":
        is_sql, is_params = _is_clause(
            node.value,
            now_sec=now_sec,
            due_day_index=due_day_index,
            alias="c",
        )
        return _Clause(where=is_sql, params=is_params)

    if node.kind == "flag":
        return _Clause(where="(c.flags & 7) = ?", params=[int(node.value)])

    if node.kind == "prop":
        prop_sql, prop_params = _prop_clause(node, alias="c")
        return _Clause(where=prop_sql, params=prop_params)

    raise ValueError(f"Unsupported card filter kind: {node.kind}")


def _compile_note_filter(node: FilterNode, *, now_sec: int, due_day_index: int) -> _Clause:
    if node.kind == "text":
        if not node.value:
            return _Clause(where="1=1", params=[])
        return _Clause(
            where="n.flds LIKE ? ESCAPE '\\'",
            params=[f"%{_escape_like(node.value)}%"],
        )

    if node.kind == "nid":
        return _Clause(where="n.id = ?", params=[int(node.value)])

    if node.kind == "tag":
        tag_pattern = _glob_to_like(node.value)
        return _Clause(
            where="n.tags LIKE ? ESCAPE '\\'",
            params=[f"% {tag_pattern} %"],
        )

    if node.kind == "notetype":
        return _Clause(
            where="n.mid IN (SELECT id FROM notetypes WHERE name LIKE ? ESCAPE '\\')",
            params=[_glob_to_like(node.value)],
        )

    if node.kind == "added":
        cutoff = now_sec - (int(node.value) * 86400)
        return _Clause(where="n.mod >= ?", params=[cutoff])

    if node.kind == "deck":
        return _Clause(
            where=(
                "EXISTS ("
                "SELECT 1 FROM cards AS c "
                "JOIN decks AS d ON d.id = c.did "
                "WHERE c.nid = n.id AND d.name LIKE ? ESCAPE '\\'"
                ")"
            ),
            params=[_glob_to_like(node.value)],
        )

    if node.kind == "cid":
        return _Clause(
            where="EXISTS (SELECT 1 FROM cards AS c WHERE c.nid = n.id AND c.id = ?)",
            params=[int(node.value)],
        )

    if node.kind == "is":
        is_sql, is_params = _is_clause(
            node.value,
            now_sec=now_sec,
            due_day_index=due_day_index,
            alias="c",
        )
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


def _is_clause(
    value: str,
    *,
    now_sec: int,
    due_day_index: int,
    alias: str,
) -> tuple[str, list[SQLParam]]:
    if value == "new":
        return (f"{alias}.queue = 0", [])

    if value == "learn":
        return (f"{alias}.queue IN (1, 3)", [])

    if value == "review":
        return (f"{alias}.queue = 2", [])

    if value == "suspended":
        return (f"{alias}.queue = -1", [])

    if value == "buried":
        return (f"{alias}.queue IN (-2, -3)", [])

    if value == "due":
        return (
            "("
            f"{alias}.queue = 0 OR "
            f"({alias}.queue IN (1, 3) AND {alias}.due <= ?) OR "
            f"({alias}.queue = 2 AND {alias}.due <= ?)"
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


def _escape_like(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    escaped = escaped.replace("%", "\\%")
    escaped = escaped.replace("_", "\\_")
    return escaped


def _glob_to_like(value: str) -> str:
    escaped = _escape_like(value)
    return escaped.replace("*", "%")