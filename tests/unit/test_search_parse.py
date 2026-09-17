"""Parser-level pins for ``core.search``: prefix detection, the ``\\:`` escape and
quoting. These are pure string logic, so they live here rather than behind SQLite.
"""

from __future__ import annotations

import pytest

from anki_cli.core.search import (
    AndNode,
    FilterNode,
    NotNode,
    SearchParseError,
    parse,
    tokenize,
)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # Escaped colon is literal text; the tokenizer must not split on it.
        (r"a\:b", FilterNode(kind="text", value="a:b")),
        (r"\:foo", FilterNode(kind="text", value=":foo")),
        (r"12\:30", FilterNode(kind="text", value="12:30")),
        # A leading colon has no prefix: plain text.
        (":x", FilterNode(kind="text", value=":x")),
        # Only the first unescaped colon separates prefix from value.
        ("tag:a:b", FilterNode(kind="tag", value="a:b")),
        (r"tag:a\:b", FilterNode(kind="tag", value="a:b")),
        # Quotes do not protect the colon (Anki: "deck:x" is still a deck search).
        ('"deck:My Deck"', FilterNode(kind="deck", value="My Deck")),
        ('deck:"My Deck"', FilterNode(kind="deck", value="My Deck")),
        ("deck:'My Deck'", FilterNode(kind="deck", value="My Deck")),
        # Leading whitespace inside the quotes exercises the separator shift.
        ('" deck:Lang"', FilterNode(kind="deck", value="Lang")),
        ('"  tag:x  "', FilterNode(kind="tag", value="x")),
        # Prefix is case-insensitive; Anki's ``note:`` spelling is accepted.
        ("Deck:X", FilterNode(kind="deck", value="X")),
        ("NOTE:Basic", FilterNode(kind="notetype", value="Basic")),
        ("notetype:Basic", FilterNode(kind="notetype", value="Basic")),
        # added: clamps 0 to 1 like rslib.
        ("added:0", FilterNode(kind="added", value="1")),
        ("added:7", FilterNode(kind="added", value="7")),
        ("-deck:Lang", NotNode(child=FilterNode(kind="deck", value="Lang"))),
    ],
)
def test_parse_prefix_and_escape_handling(query: str, expected: object) -> None:
    assert parse(query) == expected


def test_quoted_prefix_value_with_colon_inside_the_quotes() -> None:
    # 'a:b:c' -> prefix 'a' is unknown, so this must be rejected, not text-searched.
    with pytest.raises(SearchParseError, match="Unsupported filter 'a:'"):
        parse('"a:b:c"')


@pytest.mark.parametrize(
    ("query", "position", "prefix"),
    [
        ("card:1", 0, "card"),
        ("tag:foo card:1", 8, "card"),
        ("(tag:foo OR rated:1)", 12, "rated"),
        ("front:dog", 0, "front"),
        # Only the first colon was escaped; the second still separates, so the
        # prefix is the literal "foo:" (Anki would treat it as a field name too).
        (r"foo\::bar", 0, "foo:"),
        # A quoted section followed by a colon is still a prefix.
        ('"ab":c', 0, "ab"),
    ],
)
def test_unknown_prefix_reports_its_position(query: str, position: int, prefix: str) -> None:
    with pytest.raises(SearchParseError) as excinfo:
        parse(query)
    assert excinfo.value.position == position
    assert f"Unsupported filter '{prefix}:'" in str(excinfo.value)
    assert r"'\:'" in str(excinfo.value)


@pytest.mark.parametrize("query", ["added:-1", "added:-30"])
def test_negative_added_is_rejected(query: str) -> None:
    with pytest.raises(SearchParseError, match="non-negative"):
        parse(query)


def test_deck_current_is_rejected() -> None:
    with pytest.raises(SearchParseError, match="deck:current is not supported"):
        parse("deck:Current")


def test_tokenizer_records_first_unescaped_colon_index() -> None:
    (tok, _eof) = tokenize(r'"a\:b:c"')
    assert tok.value == "a:b:c"
    assert tok.separator == 3  # the second colon, the first one was escaped

    (tok, _eof) = tokenize(r"foo\::bar")
    assert tok.value == "foo::bar"
    assert tok.separator == 4

    (tok, _eof) = tokenize(r"a\:b")
    assert tok.separator is None


def test_implicit_and_of_mixed_terms() -> None:
    assert parse(r"deck:X 12\:30") == AndNode(
        children=(
            FilterNode(kind="deck", value="X"),
            FilterNode(kind="text", value="12:30"),
        )
    )
