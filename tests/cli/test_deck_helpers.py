from __future__ import annotations

import pytest

from anki_cli.cli.commands.deck import _deck_chain, _parse_step_values


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("A", ["A"]),
        ("A::B", ["A", "A::B"]),
        ("A::B::C", ["A", "A::B", "A::B::C"]),
        ("  A  ", ["A"]),
        ("  A  ::  B  ", ["A", "A::B"]),
    ],
)
def test_deck_chain_valid(name: str, expected: list[str]) -> None:
    assert _deck_chain(name) == expected


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "A::",
        "::A",
        "A::::B",
        "A:: ::B",
    ],
)
def test_deck_chain_invalid(name: str) -> None:
    with pytest.raises(ValueError):
        _deck_chain(name)


def test_parse_step_values_none_returns_none() -> None:
    assert _parse_step_values(None) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", []),
        ("   ", []),
        ("1", [1.0]),
        ("1,2,3", [1.0, 2.0, 3.0]),
        (" 1 , 2.5 , 3 ", [1.0, 2.5, 3.0]),
        ("1,,2", [1.0, 2.0]),
    ],
)
def test_parse_step_values_valid(raw: str, expected: list[float]) -> None:
    assert _parse_step_values(raw) == expected


@pytest.mark.parametrize("raw", ["a", "1,b", "1, 2, nope"])
def test_parse_step_values_invalid(raw: str) -> None:
    with pytest.raises(ValueError):
        _parse_step_values(raw)