from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

type JSONPrimitive = str | int | float | bool | None
type JSONValue = JSONPrimitive | Mapping[str, "JSONValue"] | Sequence["JSONValue"]


class Meta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str
    backend: str
    collection: str | None = None
    timestamp: str


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