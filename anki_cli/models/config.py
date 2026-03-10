from __future__ import annotations

from pydantic import BaseModel, Field


class CollectionConfig(BaseModel):
    path: str = "~/.local/share/anki-cli/collection.db"
    anki_profile: str = "User 1"


class BackendConfig(BaseModel):
    prefer: str = Field(default="auto")
    ankiconnect_url: str = "http://localhost:8765"
    allow_non_localhost: bool = False


class DisplayConfig(BaseModel):
    default_output: str = "table"
    color: bool = True
    day_boundary_hour: int = 4


class BackupConfig(BaseModel):
    enabled: bool = True
    max_backups: int = 30
    path: str = "~/.local/share/anki-cli/backups"


class ReviewConfig(BaseModel):
    show_timer: bool = False
    max_answer_seconds: int = 60


class AppConfig(BaseModel):
    collection: CollectionConfig = Field(default_factory=CollectionConfig)
    backend: BackendConfig = Field(default_factory=BackendConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)