from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, Field

DEFAULT_ANKICONNECT_URL: Final[str] = "http://localhost:8765"
# The Textual theme the TUI registers (tui/colors.py); any built-in theme name works too.
DEFAULT_THEME: Final[str] = "anki-tokyo-night"

# Single source of truth for the allowed values; CLI choices, config
# validation, and detection all derive their sets via ``typing.get_args``.
BackendPreference = Literal["auto", "ankiconnect", "direct"]
OutputFormat = Literal["table", "json", "md", "csv", "plain"]


class CollectionConfig(BaseModel):
    path: str | None = None
    anki_profile: str | None = None


class BackendConfig(BaseModel):
    prefer: BackendPreference = Field(default="auto")
    ankiconnect_url: str = DEFAULT_ANKICONNECT_URL
    allow_non_localhost: bool = False


class DisplayConfig(BaseModel):
    default_output: OutputFormat = "table"
    color: bool = True
    theme: str = DEFAULT_THEME


class AppConfig(BaseModel):
    collection: CollectionConfig = Field(default_factory=CollectionConfig)
    backend: BackendConfig = Field(default_factory=BackendConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
