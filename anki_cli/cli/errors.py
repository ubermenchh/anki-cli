"""Exception -> JSON-envelope mapping for the CLI entry point (#27).

Commands emit their own envelopes for the failures they anticipate. Anything
that escapes — a Click usage error, a connection dropping mid-command, a locked
collection, Ctrl-C, a bug — used to reach the user as Click's plain-text usage
message or a Python traceback, even under ``--format json``. ``classify`` gives
every escaping exception an ``error.code`` and an exit code so the entry point
can render it through the same formatter.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

import click

from anki_cli.backends.ankiconnect import (
    AnkiConnectError,
    AnkiConnectUnavailableError,
)
from anki_cli.backends.detect import DetectionError
from anki_cli.backends.factory import BackendFactoryError
from anki_cli.config_runtime import ConfigError
from anki_cli.core.search import SearchParseError
from anki_cli.core.template import TemplateParseError
from anki_cli.db.anki_direct import DirectWriteBlockedError, UnsupportedCollectionError
from anki_cli.models.output import ErrorCode, ExitCode, JSONValue

# Domain and stdlib exceptions only; Click's own (``UsageError``, ``Abort``)
# and ``DetectionError`` / sqlite lock errors are handled by ``classify``
# directly because they carry extra state. Most-specific-first ordering is
# not required: ``classify`` walks the exception's MRO and takes the first
# class present here, so a subclass entry always wins over its base.
ERROR_MAP: dict[type[BaseException], tuple[ErrorCode, ExitCode]] = {
    # Input
    SearchParseError: (ErrorCode.INVALID_INPUT, ExitCode.INVALID_INPUT),
    TemplateParseError: (ErrorCode.INVALID_INPUT, ExitCode.INVALID_INPUT),
    ConfigError: (ErrorCode.INVALID_CONFIG, ExitCode.INVALID_INPUT),
    # Backend reachability
    BackendFactoryError: (ErrorCode.BACKEND_UNAVAILABLE, ExitCode.BACKEND_UNAVAILABLE),
    NotImplementedError: (ErrorCode.BACKEND_UNAVAILABLE, ExitCode.BACKEND_UNAVAILABLE),
    AnkiConnectUnavailableError: (ErrorCode.BACKEND_UNAVAILABLE, ExitCode.BACKEND_UNAVAILABLE),
    UnsupportedCollectionError: (ErrorCode.BACKEND_UNAVAILABLE, ExitCode.BACKEND_UNAVAILABLE),
    DirectWriteBlockedError: (ErrorCode.COLLECTION_LOCKED, ExitCode.BACKEND_UNAVAILABLE),
    # Operation. The backends raise bare LookupError for "not found"; its two
    # stdlib subclasses are how *bugs* surface (a missing dict key, an index
    # past the end) and must not read as a user-facing miss. Subclass entries
    # win the MRO walk, so these two override the base row.
    LookupError: (ErrorCode.ENTITY_NOT_FOUND, ExitCode.ENTITY_NOT_FOUND),
    KeyError: (ErrorCode.INTERNAL_ERROR, ExitCode.BACKEND_OPERATION_FAILED),
    IndexError: (ErrorCode.INTERNAL_ERROR, ExitCode.BACKEND_OPERATION_FAILED),
    AnkiConnectError: (ErrorCode.BACKEND_OPERATION_FAILED, ExitCode.BACKEND_OPERATION_FAILED),
    ValueError: (ErrorCode.BACKEND_OPERATION_FAILED, ExitCode.BACKEND_OPERATION_FAILED),
    sqlite3.DatabaseError: (ErrorCode.BACKEND_OPERATION_FAILED, ExitCode.BACKEND_OPERATION_FAILED),
}

# SQLite's wording for SQLITE_BUSY / SQLITE_LOCKED. It never says "busy", and a
# bare substring like that would misfile "no such table: busy_queue".
_LOCK_MARKERS = ("database is locked", "database table is locked", "database schema is locked")


@dataclass(frozen=True, slots=True)
class ClassifiedError:
    code: ErrorCode
    exit_code: int
    message: str
    details: dict[str, JSONValue]


def classify(exc: BaseException) -> ClassifiedError:
    """Map any exception to an envelope code, exit code, message and details."""
    details: dict[str, JSONValue] = {}

    if isinstance(exc, DetectionError):
        # Carries its own exit code (3 = nothing found, 7 = forced backend down).
        return ClassifiedError(
            ErrorCode.BACKEND_UNAVAILABLE, int(exc.exit_code), str(exc), details
        )

    if isinstance(exc, sqlite3.OperationalError):
        text = str(exc)
        if any(marker in text.lower() for marker in _LOCK_MARKERS):
            return ClassifiedError(
                ErrorCode.COLLECTION_LOCKED,
                ExitCode.BACKEND_UNAVAILABLE,
                f"Collection is locked: {text}. Close Anki Desktop (or wait for sync) and retry.",
                details,
            )

    if isinstance(exc, click.UsageError):
        message = exc.format_message()
        if exc.ctx is not None:
            details["usage"] = exc.ctx.get_usage()
        return ClassifiedError(ErrorCode.INVALID_INPUT, exc.exit_code, message, details)

    if isinstance(exc, click.ClickException):
        return ClassifiedError(
            ErrorCode.INVALID_INPUT, exc.exit_code, exc.format_message(), details
        )

    if isinstance(exc, (click.Abort, KeyboardInterrupt)):
        # Click turns KeyboardInterrupt into Abort before it reaches the entry
        # point; the tuple keeps classify() honest for direct callers.
        return ClassifiedError(ErrorCode.INTERRUPTED, ExitCode.INTERRUPTED, "Interrupted.", details)

    for klass in type(exc).__mro__:
        mapped = ERROR_MAP.get(klass)
        if mapped is None:
            continue
        code, exit_code = mapped
        if code is ErrorCode.INTERNAL_ERROR:
            break  # explicitly filed as a bug: fall through to the bug shape
        return ClassifiedError(code, exit_code, str(exc) or klass.__name__, details)

    # A bug, not a user or environment error. Name the type so a report is
    # useful; the traceback is one env var away.
    details["exception"] = type(exc).__name__
    return ClassifiedError(
        ErrorCode.INTERNAL_ERROR,
        ExitCode.BACKEND_OPERATION_FAILED,
        f"Unexpected error: {type(exc).__name__}: {exc}",
        details,
    )


def debug_tracebacks_enabled() -> bool:
    return os.environ.get("ANKI_CLI_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}

