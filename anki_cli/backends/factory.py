from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from anki_cli.backends.ankiconnect import AnkiConnectBackend, AnkiConnectError
from anki_cli.backends.direct import DirectBackend
from anki_cli.backends.protocol import AnkiBackend
from anki_cli.models.config import AppConfig


class BackendFactoryError(RuntimeError):
    """Base backend factory error."""


class BackendNotImplementedError(BackendFactoryError):
    """Raised when backend exists in design but is not implemented yet."""


def create_backend_from_context(obj: dict[str, Any]) -> AnkiBackend:
    backend_name = str(obj.get("backend", "")).strip().lower()
    collection_path = _coerce_path(obj.get("collection_path"))
    app_config = obj.get("app_config")

    ankiconnect_url = "http://localhost:8765"
    if isinstance(app_config, AppConfig):
        ankiconnect_url = app_config.backend.ankiconnect_url

    if backend_name == "ankiconnect":
        try:
            return AnkiConnectBackend(
                url=ankiconnect_url,
                collection_path=collection_path,
                verify_version=True,
            )
        except AnkiConnectError as exc:
            raise BackendFactoryError(str(exc)) from exc

    if backend_name == "direct":
        if collection_path is None:
            raise BackendFactoryError("Direct backend requires a collection path.")
        try:
            return DirectBackend(collection_path)
        except FileNotFoundError as exc:
            raise BackendFactoryError(str(exc)) from exc

    if backend_name in "standalone":
        raise BackendNotImplementedError(
            f"Backend '{backend_name}' is detected but not implemented yet."
        )

    raise BackendFactoryError(f"Unknown backend '{backend_name}'.")


@contextmanager
def backend_session_from_context(obj: dict[str, Any]) -> Generator[AnkiBackend, None, None]:
    backend = create_backend_from_context(obj)
    try:
        yield backend
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()


def _coerce_path(value: object) -> Path | None:
    if isinstance(value, Path):
        return value
    if isinstance(value, str) and value.strip():
        return Path(value).expanduser().resolve()
    return None