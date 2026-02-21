from __future__ import annotations

import pytest
from pydantic import ValidationError

from anki_cli.models.config import AppConfig
from anki_cli.models.output import ErrorInfo, ErrorResponse, Meta, SuccessResponse


def _meta() -> Meta:
    return Meta(command="test", backend="direct", timestamp="2026-02-20T00:00:00Z")


def test_app_config_defaults() -> None:
    cfg = AppConfig()

    assert cfg.collection.path == "~/.local/share/anki-cli/collection.db"
    assert cfg.collection.anki_profile == "User 1"
    assert cfg.backend.prefer == "auto"
    assert cfg.backend.ankiconnect_url == "http://localhost:8765"
    assert cfg.display.default_output == "table"
    assert cfg.display.color is True
    assert cfg.display.day_boundary_hour == 4
    assert cfg.backup.enabled is True
    assert cfg.backup.max_backups == 30
    assert cfg.backup.path == "~/.local/share/anki-cli/backups"
    assert cfg.review.show_timer is False
    assert cfg.review.max_answer_seconds == 60


def test_app_config_instances_are_independent() -> None:
    first = AppConfig()
    second = AppConfig()

    first.collection.path = "/tmp/one.db"
    first.review.max_answer_seconds = 10

    assert second.collection.path == "~/.local/share/anki-cli/collection.db"
    assert second.review.max_answer_seconds == 60


def test_meta_collection_defaults_to_none() -> None:
    meta = _meta()
    assert meta.collection is None


def test_meta_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        Meta(
            command="x",
            backend="direct",
            timestamp="2026-02-20T00:00:00Z",
            extra_field="nope",  # type: ignore[call-arg]
        )


def test_success_response_accepts_nested_json_value() -> None:
    payload = {
        "id": 1,
        "items": ["a", 2, {"ok": True, "nested": [None, 3.14]}],
        "obj": {"k": "v"},
    }

    resp = SuccessResponse(data=payload, meta=_meta())

    assert resp.ok is True
    assert resp.data == payload
    assert resp.meta.command == "test"


def test_success_response_ok_literal_enforced() -> None:
    with pytest.raises(ValidationError):
        SuccessResponse(ok=False, data={}, meta=_meta())  # type: ignore[arg-type]


def test_success_response_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        SuccessResponse(data={}, meta=_meta(), unexpected=1)  # type: ignore[call-arg]


def test_error_info_defaults_and_instances_are_independent() -> None:
    first = ErrorInfo(code="E1", message="one")
    second = ErrorInfo(code="E2", message="two")

    first.details["k"] = "v"

    assert second.details == {}
    assert first.details == {"k": "v"}


def test_error_info_rejects_non_json_value_in_details() -> None:
    with pytest.raises(ValidationError):
        ErrorInfo(code="E", message="bad", details={"x": object()})  # type: ignore[arg-type]


def test_error_response_ok_literal_enforced() -> None:
    with pytest.raises(ValidationError):
        ErrorResponse(
            ok=True,  # type: ignore[arg-type]
            error=ErrorInfo(code="E", message="boom"),
            meta=_meta(),
        )


def test_error_response_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ErrorResponse(
            error=ErrorInfo(code="E", message="boom"),
            meta=_meta(),
            unexpected=1,  # type: ignore[call-arg]
        )