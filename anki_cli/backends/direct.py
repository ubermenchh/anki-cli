from __future__ import annotations

from pathlib import Path

from anki_cli.backends.protocol import AnkiBackend
from anki_cli.db.store import AnkiDirectStore


class DirectBackend(AnkiDirectStore, AnkiBackend):
    """The direct SQLite backend.

    ``AnkiDirectStore`` already has every ``AnkiBackend`` method with the
    protocol's signatures; this class only adds the backend identity the CLI
    and TUI branch on. The store's ``__init__`` resolves the path and refuses a
    missing file or a pre-schema-18 collection.
    """

    name = "direct"
    supports_scheduler_introspection = True

    @property
    def collection_path(self) -> Path:
        return self.db_path
