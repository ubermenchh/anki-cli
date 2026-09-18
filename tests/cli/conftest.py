"""Shared helpers for driving commands through ``CliRunner`` (#37).

Eight files used to carry identical copies of these four functions. They are
plain module-level helpers (imported, not fixtures) so a test reads the same
way it always did: ``_base_obj()``, ``_success_payload(result)``, …
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import pytest

import anki_cli.backends.factory as factory_mod


def base_obj(**overrides: Any) -> dict[str, Any]:
    """The ``ctx.obj`` the group callback would have built for ``--format json
    --backend direct``. Pass ``yes=True`` for the destructive commands."""
    base: dict[str, Any] = {
        "format": "json",
        "backend": "direct",
        "collection_path": None,
        "no_color": True,
        "copy": False,
        "yes": False,
    }
    base.update(overrides)
    return base


def success_payload(result) -> dict[str, Any]:
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    return payload


def error_payload(result) -> dict[str, Any]:
    """Error envelopes go to stderr only (#27); parsing stderr alone means a
    stray stdout line fails cleanly instead of as a JSONDecodeError."""
    assert result.exit_code != 0
    payload = json.loads(result.stderr.strip())
    assert payload["ok"] is False
    return payload


def patch_session(monkeypatch: pytest.MonkeyPatch, backend: Any) -> None:
    """Make every command's backend session yield ``backend``. Patches the
    factory module, which is where ``@anki_command`` looks it up."""

    @contextmanager
    def fake_session(obj: dict[str, Any]):
        yield backend

    monkeypatch.setattr(factory_mod, "backend_session_from_context", fake_session)


def failing_session(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    """Make the backend session raise ``exc`` on open."""

    def boom(obj: dict[str, Any]):
        raise exc

    monkeypatch.setattr(factory_mod, "backend_session_from_context", boom)
