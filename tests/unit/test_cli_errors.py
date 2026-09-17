from __future__ import annotations

import sqlite3

import click
import pytest

from anki_cli.backends.ankiconnect import AnkiConnectAPIError, AnkiConnectUnavailableError
from anki_cli.backends.detect import DetectionError
from anki_cli.cli.errors import classify, peek_output_options
from anki_cli.core.search import SearchParseError
from anki_cli.db.anki_direct import DuplicateNoteError, EmptyNoteError
from anki_cli.models.output import ErrorCode, ExitCode


@pytest.mark.parametrize(
    ("exc", "code", "exit_code"),
    [
        (SearchParseError("bad", query="q"), ErrorCode.INVALID_INPUT, 2),
        (DetectionError("none found", exit_code=3), ErrorCode.BACKEND_UNAVAILABLE, 3),
        (DetectionError("forced down", exit_code=7), ErrorCode.BACKEND_UNAVAILABLE, 7),
        (AnkiConnectUnavailableError("timeout"), ErrorCode.BACKEND_UNAVAILABLE, 7),
        (AnkiConnectAPIError("addNote", "dup"), ErrorCode.BACKEND_OPERATION_FAILED, 1),
        (sqlite3.OperationalError("database is locked"), ErrorCode.COLLECTION_LOCKED, 7),
        (sqlite3.OperationalError("unable to open database file"),
         ErrorCode.BACKEND_OPERATION_FAILED, 1),
        (sqlite3.DatabaseError("file is not a database"), ErrorCode.BACKEND_OPERATION_FAILED, 1),
        (KeyError("k"), ErrorCode.ENTITY_NOT_FOUND, 4),  # LookupError subclass
        (DuplicateNoteError(notetype="Basic", duplicate_ids=[1]),
         ErrorCode.BACKEND_OPERATION_FAILED, 1),
        (EmptyNoteError(notetype="Basic", field_name="Front"),
         ErrorCode.BACKEND_OPERATION_FAILED, 1),
        (click.Abort(), ErrorCode.INTERRUPTED, 130),
        (KeyboardInterrupt(), ErrorCode.INTERRUPTED, 130),
        (OSError("disk"), ErrorCode.INTERNAL_ERROR, 1),
    ],
)
def test_classify(exc: BaseException, code: ErrorCode, exit_code: int) -> None:
    result = classify(exc)
    assert result.code == code
    assert result.exit_code == exit_code


def test_classify_subclass_beats_base_regardless_of_dict_order() -> None:
    """AnkiConnectUnavailableError is an AnkiConnectError; the MRO walk must take
    the subclass entry (exit 7) even though the base entry (exit 1) also matches."""
    assert classify(AnkiConnectUnavailableError("x")).exit_code == 7
    assert classify(AnkiConnectAPIError("a", "b")).exit_code == 1


def test_classify_usage_error_carries_usage_line() -> None:
    @click.command("probe")
    def probe() -> None: ...

    ctx = click.Context(probe, info_name="probe")
    result = classify(click.UsageError("No such option: --x", ctx=ctx))

    assert result.code == ErrorCode.INVALID_INPUT
    assert result.exit_code == 2
    assert result.message == "No such option: --x"
    assert result.details["usage"] == "Usage: probe [OPTIONS]"


def test_classify_internal_error_names_type() -> None:
    result = classify(ZeroDivisionError("division by zero"))
    assert result.code == ErrorCode.INTERNAL_ERROR
    assert result.exit_code == ExitCode.BACKEND_OPERATION_FAILED
    assert result.details == {"exception": "ZeroDivisionError"}
    assert "ZeroDivisionError: division by zero" in result.message


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], (None, False)),
        (["--format", "json", "x"], ("json", False)),
        (["x", "--format=md"], ("md", False)),
        (["--no-color", "x", "--format", "json"], ("json", True)),
        (["x", "--", "--format", "json"], (None, False)),
        (["--format"], (None, False)),  # dangling: nothing to read
    ],
)
def test_peek_output_options(argv: list[str], expected: tuple[str | None, bool]) -> None:
    assert peek_output_options(argv) == expected
