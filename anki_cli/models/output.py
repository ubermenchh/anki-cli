from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import IntEnum, StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

type JSONPrimitive = str | int | float | bool | None
type JSONValue = JSONPrimitive | Mapping[str, "JSONValue"] | Sequence["JSONValue"]


class ExitCode(IntEnum):
    """Process exit codes. The README / SKILL.md tables are generated from this."""

    OK = 0
    BACKEND_OPERATION_FAILED = 1
    INVALID_INPUT = 2
    NO_BACKEND_FOUND = 3
    ENTITY_NOT_FOUND = 4
    BACKEND_UNAVAILABLE = 7
    INTERRUPTED = 130


EXIT_CODE_MEANINGS: dict[ExitCode, str] = {
    ExitCode.OK: "Success",
    ExitCode.BACKEND_OPERATION_FAILED: "Backend operation failed (including unexpected errors)",
    ExitCode.INVALID_INPUT: (
        "Invalid input, usage error, confirmation required, or unsupported operation"
    ),
    ExitCode.NO_BACKEND_FOUND: (
        "No Anki backend found (auto mode could not reach AnkiConnect or find a collection)"
    ),
    ExitCode.ENTITY_NOT_FOUND: "Entity not found",
    ExitCode.BACKEND_UNAVAILABLE: "Backend unavailable or collection locked",
    ExitCode.INTERRUPTED: "Interrupted (Ctrl-C)",
}


class ErrorCode(StrEnum):
    """``error.code`` values in the JSON envelope."""

    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    COLLECTION_LOCKED = "COLLECTION_LOCKED"
    INVALID_INPUT = "INVALID_INPUT"
    INVALID_CONFIG = "INVALID_CONFIG"
    ENTITY_NOT_FOUND = "ENTITY_NOT_FOUND"
    BACKEND_OPERATION_FAILED = "BACKEND_OPERATION_FAILED"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    UNDO_EMPTY = "UNDO_EMPTY"
    TUI_NOT_AVAILABLE = "TUI_NOT_AVAILABLE"
    UNSUPPORTED_BACKEND = "UNSUPPORTED_BACKEND"
    INTERRUPTED = "INTERRUPTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class Meta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str
    backend: str
    collection: str | None = None
    timestamp: str
    warnings: list[str] = Field(default_factory=list)
    """Non-fatal notices the caller should surface (e.g. "next sync is a full upload")."""


class ErrorInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, JSONValue] = Field(default_factory=dict)


class SuccessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True] = True
    data: JSONValue
    meta: Meta


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[False] = False
    error: ErrorInfo
    meta: Meta
