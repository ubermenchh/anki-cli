from __future__ import annotations

from typing import Final

from pydantic import BaseModel, Field

DEFAULT_ANKICONNECT_URL: Final[str] = "http://localhost:8765"


class CollectionConfig(BaseModel):
    path: str = "~/.local/share/anki-cli/collection.db"
    anki_profile: str = "User 1"


class BackendConfig(BaseModel):
    prefer: str = Field(default="auto")
    ankiconnect_url: str = DEFAULT_ANKICONNECT_URL
    allow_non_localhost: bool = False


class DisplayConfig(BaseModel):
    default_output: str = "table"
    color: bool = True


class AppConfig(BaseModel):
    collection: CollectionConfig = Field(default_factory=CollectionConfig)
    backend: BackendConfig = Field(default_factory=BackendConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
