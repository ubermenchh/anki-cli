from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import anki_cli.backends.factory as factory_mod
from anki_cli.backends.factory import (
    BackendFactoryError,
    BackendNotImplementedError,
    backend_session_from_context,
    create_backend_from_context,
)
from anki_cli.models.config import AppConfig


def test_create_backend_unknown_backend_raises() -> None:
    with pytest.raises(BackendFactoryError, match="Unknown backend"):
        create_backend_from_context({"backend": "nope"})


def test_create_backend_standalone_not_implemented() -> None:
    with pytest.raises(BackendNotImplementedError, match="not implemented"):
        create_backend_from_context({"backend": "standalone"})


def test_create_backend_direct_requires_collection_path() -> None:
    with pytest.raises(BackendFactoryError, match="requires a collection path"):
        create_backend_from_context({"backend": "direct", "collection_path": None})


def test_create_backend_direct_missing_file_maps_to_factory_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"

    with pytest.raises(
        BackendFactoryError, 
        match=r"Direct collection not found|Direct DB not found"
    ):
        create_backend_from_context({"backend": "direct", "collection_path": missing})


def test_create_backend_direct_success(tmp_path: Path) -> None:
    db = tmp_path / "collection.db"
    db.touch()

    backend = create_backend_from_context({"backend": "direct", "collection_path": db})

    assert getattr(backend, "name", None) == "direct"
    assert getattr(backend, "collection_path", None) == db.resolve()


def test_create_backend_ankiconnect_uses_default_url(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class DummyBackend:
        name = "ankiconnect"

        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(factory_mod, "AnkiConnectBackend", DummyBackend)

    backend = create_backend_from_context({"backend": "ankiconnect", "collection_path": None})

    assert getattr(backend, "name", None) == "ankiconnect"
    assert captured["url"] == "http://localhost:8765"
    assert captured["verify_version"] is True
    assert captured["collection_path"] is None


def test_create_backend_ankiconnect_reads_url_from_app_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class DummyBackend:
        name = "ankiconnect"

        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(factory_mod, "AnkiConnectBackend", DummyBackend)

    app_config = AppConfig()
    app_config.backend.ankiconnect_url = "http://127.0.0.1:9999"

    create_backend_from_context(
        {
            "backend": "ankiconnect",
            "collection_path": None,
            "app_config": app_config,
        }
    )

    assert captured["url"] == "http://127.0.0.1:9999"


def test_create_backend_ankiconnect_error_is_mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**kwargs: Any) -> Any:
        raise factory_mod.AnkiConnectError("cannot connect")

    monkeypatch.setattr(factory_mod, "AnkiConnectBackend", boom)

    with pytest.raises(BackendFactoryError, match="cannot connect"):
        create_backend_from_context({"backend": "ankiconnect"})


def test_coerce_path_from_string(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = tmp_path / "collection.db"
    db.touch()

    captured: dict[str, Any] = {}

    class DummyDirect:
        name = "direct"

        def __init__(self, path: Path) -> None:
            captured["path"] = path

    monkeypatch.setattr(factory_mod, "DirectBackend", DummyDirect)

    create_backend_from_context({"backend": "direct", "collection_path": str(db)})

    assert captured["path"] == db.resolve()


def test_backend_session_from_context_closes_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = {"value": False}

    class DummyBackend:
        def close(self) -> None:
            closed["value"] = True

    monkeypatch.setattr(factory_mod, "create_backend_from_context", lambda obj: DummyBackend())

    with backend_session_from_context({"backend": "ankiconnect"}) as backend:
        assert isinstance(backend, DummyBackend)

    assert closed["value"] is True


def test_backend_session_from_context_no_close_method(monkeypatch: pytest.MonkeyPatch) -> None:
    class DummyBackend:
        pass

    monkeypatch.setattr(factory_mod, "create_backend_from_context", lambda obj: DummyBackend())

    with backend_session_from_context({"backend": "ankiconnect"}) as backend:
        assert isinstance(backend, DummyBackend)