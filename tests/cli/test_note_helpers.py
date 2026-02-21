from __future__ import annotations

import click
import pytest

from anki_cli.cli.commands.note import _parse_dynamic_fields, _parse_tags


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, []),
        ("", []),
        ("  ", []),
        ("foo", ["foo"]),
        ("foo bar", ["bar", "foo"]),
        ("foo,bar", ["bar", "foo"]),
        (" foo, bar baz  ", ["bar", "baz", "foo"]),
        ("foo foo,foo", ["foo"]),
    ],
)
def test_parse_tags(raw: str | None, expected: list[str]) -> None:
    assert _parse_tags(raw) == expected


def test_parse_dynamic_fields_happy_path() -> None:
    args = ["--Front", "Question", "--Back", "Answer"]
    fields = _parse_dynamic_fields(args)

    assert fields == {"Front": "Question", "Back": "Answer"}


def test_parse_dynamic_fields_allows_overwrite_last_value_wins() -> None:
    args = ["--Front", "Q1", "--Front", "Q2"]
    fields = _parse_dynamic_fields(args)

    assert fields == {"Front": "Q2"}


def test_parse_dynamic_fields_allows_values_with_equal_sign() -> None:
    args = ["--Text", "a=b=c", "--Extra", "x"]
    fields = _parse_dynamic_fields(args)

    assert fields == {"Text": "a=b=c", "Extra": "x"}


def test_parse_dynamic_fields_empty_list_returns_empty_dict() -> None:
    assert _parse_dynamic_fields([]) == {}


def test_parse_dynamic_fields_rejects_unexpected_token() -> None:
    with pytest.raises(click.ClickException, match="Unexpected field token"):
        _parse_dynamic_fields(["Front", "Question"])


def test_parse_dynamic_fields_rejects_empty_field_name() -> None:
    with pytest.raises(click.ClickException, match="Empty field name"):
        _parse_dynamic_fields(["--", "Question"])


def test_parse_dynamic_fields_rejects_missing_value_at_end() -> None:
    with pytest.raises(click.ClickException, match="Missing value for field 'Front'"):
        _parse_dynamic_fields(["--Front"])


def test_parse_dynamic_fields_rejects_missing_value_before_next_option() -> None:
    with pytest.raises(click.ClickException, match="Missing value for field 'Front'"):
        _parse_dynamic_fields(["--Front", "--Back", "A"])