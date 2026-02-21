import pytest

from anki_cli.cli.params import preprocess_argv


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["note:add", "deck=Default", "Front=Q", "Back=A"],
            ["note:add", "--deck", "Default", "--Front", "Q", "--Back", "A"],
        ),
        (
            ["note:add", "--deck", "Default", "Front=Q"],
            ["note:add", "--deck", "Default", "--Front", "Q"],
        ),
        (
            ["note:add", "--", "Front=Q", "Back=A"],
            ["note:add", "--", "Front=Q", "Back=A"],
        ),
        (
            ["note:add", "--foo=bar", "Front=Q"],
            ["note:add", "--foo=bar", "--Front", "Q"],
        ),
        (
            ["note:add", "=value", "Front=Q"],
            ["note:add", "=value", "--Front", "Q"],
        ),
        (
            ["note:add", "bad key=value", "Front=Q"],
            ["note:add", "bad key=value", "--Front", "Q"],
        ),
        (
            ["note:add", "Front=a=b"],
            ["note:add", "--Front", "a=b"],
        ),
        (
            ["note:add", "Front="],
            ["note:add", "--Front", ""],
        ),
        (
            [],
            [],
        ),
    ],
)
def test_preprocess_argv(argv: list[str], expected: list[str]) -> None:
    assert preprocess_argv(argv) == expected