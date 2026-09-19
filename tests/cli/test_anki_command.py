"""``@anki_command`` in isolation (#29): the contract every converted command
now relies on, pinned without any real backend."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import click
import pytest
from click.testing import CliRunner

import anki_cli.backends.factory as factory_mod
import anki_cli.cli.formatter as formatter_mod
from anki_cli.backends.ankiconnect import AnkiConnectAPIError
from anki_cli.backends.factory import BackendFactoryError
from anki_cli.cli import command as command_mod
from anki_cli.cli.command import CommandContext, anki_command, id_or_query
from anki_cli.db.search_sql import SearchParseError


def _obj(**over: Any) -> dict[str, Any]:
    base = {"format": "json", "backend": "direct", "collection_path": None,
            "no_color": True, "copy": False, "yes": False}
    base.update(over)
    return base


class _Backend:
    def __init__(self) -> None:
        self.closed = False
        self.calls: list[str] = []

    def hello(self) -> dict[str, Any]:
        self.calls.append("hello")
        return {"hi": True}

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> _Backend:
    be = _Backend()

    @contextmanager
    def fake_session(obj: dict[str, Any]):
        # Like the real one: closes on the error path too.
        try:
            yield be
        finally:
            be.close()

    monkeypatch.setattr(factory_mod, "backend_session_from_context", fake_session)
    return be


def _make(name: str, body, **kw) -> click.Command:
    return anki_command(name, register=False, **kw)(body)


def _ok(result) -> dict[str, Any]:
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _err(result) -> dict[str, Any]:
    assert result.exit_code != 0
    return json.loads(result.stderr)


# --- success path ------------------------------------------------------------------------


def test_return_value_is_the_success_data_and_session_is_closed(backend: _Backend) -> None:
    def body(cmd: CommandContext) -> dict[str, Any]:
        return cmd.backend.hello()

    result = CliRunner().invoke(_make("probe", body), [], obj=_obj())

    payload = _ok(result)
    assert payload["ok"] is True
    assert payload["data"] == {"hi": True}
    assert payload["meta"]["command"] == "probe"
    assert backend.closed is True


def test_returning_none_emits_nothing(backend: _Backend) -> None:
    def body(cmd: CommandContext) -> None:
        return None

    result = CliRunner().invoke(_make("probe", body), [], obj=_obj())
    assert result.exit_code == 0
    assert result.output == ""


def test_backend_is_lazy_so_commands_without_one_open_no_session(monkeypatch) -> None:
    def boom(obj):
        raise AssertionError("session must not open")

    monkeypatch.setattr(factory_mod, "backend_session_from_context", boom)

    def body(cmd: CommandContext) -> dict[str, Any]:
        return {"static": 1}

    assert _ok(CliRunner().invoke(_make("probe", body), [], obj=_obj()))["data"] == {"static": 1}


def test_warnings_are_forwarded_only_when_present(backend: _Backend, monkeypatch) -> None:
    seen: list[dict[str, Any]] = []

    class Formatter:
        def emit_success(self, **kw: Any) -> None:
            seen.append(kw)

        def emit_error(self, **kw: Any) -> None:
            raise AssertionError(kw)

    monkeypatch.setattr(formatter_mod, "formatter_from_ctx", lambda ctx: Formatter())

    def quiet(cmd: CommandContext) -> dict[str, Any]:
        return {}

    def loud(cmd: CommandContext) -> dict[str, Any]:
        cmd.warnings.append("heads up")
        return {}

    CliRunner().invoke(_make("q", quiet), [], obj=_obj())
    CliRunner().invoke(_make("l", loud), [], obj=_obj())

    # A formatter whose emit_success has no ``warnings`` kwarg (one test's
    # CaptureFormatter) must keep working for commands that emit none.
    assert "warnings" not in seen[0]
    assert seen[1]["warnings"] == ["heads up"]


# --- failure paths ------------------------------------------------------------------------


@pytest.mark.parametrize("exc", [BackendFactoryError("down"), NotImplementedError("nope")])
def test_backend_unavailable_is_built_in(backend: _Backend, exc: Exception) -> None:
    def body(cmd: CommandContext) -> dict[str, Any]:
        raise exc

    payload = _err(result := CliRunner().invoke(_make("probe", body), [], obj=_obj()))
    assert result.exit_code == 7
    assert payload["error"]["code"] == "BACKEND_UNAVAILABLE"
    assert payload["error"]["details"] == {"backend": "direct"}
    assert payload["meta"]["command"] == "probe"


def test_error_table_maps_by_mro_most_specific_first(backend: _Backend) -> None:
    class Special(LookupError):
        pass

    def body(cmd: CommandContext) -> dict[str, Any]:
        raise Special("missing")

    table = {LookupError: ("ENTITY_NOT_FOUND", 4), Special: ("BACKEND_OPERATION_FAILED", 1)}
    result = CliRunner().invoke(_make("probe", body, errors=table), [], obj=_obj())
    assert result.exit_code == 1
    assert _err(result)["error"]["code"] == "BACKEND_OPERATION_FAILED"


def test_unmapped_exception_escapes_for_the_entry_point_to_render(backend: _Backend) -> None:
    def body(cmd: CommandContext) -> dict[str, Any]:
        cmd.backend.hello()  # open the session first
        raise ZeroDivisionError("bug")

    result = CliRunner().invoke(_make("probe", body), [], obj=_obj())
    assert isinstance(result.exception, ZeroDivisionError)
    assert result.stderr == ""  # nothing emitted here; the entry point renders it
    assert backend.closed is True  # session still cleaned up on the way out


def test_errors_block_attaches_details_from_the_command_table(backend: _Backend) -> None:
    def body(cmd: CommandContext) -> dict[str, Any]:
        with cmd.errors(details={"why": "inner", "n": 7}):
            raise ValueError("inner")
        return {}

    table = {ValueError: ("BACKEND_OPERATION_FAILED", 1)}
    payload = _err(CliRunner().invoke(_make("probe", body, errors=table), [], obj=_obj()))
    assert payload["error"]["message"] == "inner"
    assert payload["error"]["details"] == {"why": "inner", "n": 7}


def test_fail_and_invalid_emit_and_return_an_exit(backend: _Backend) -> None:
    def body(cmd: CommandContext) -> dict[str, Any]:
        raise cmd.invalid("bad input", details={"field": "x"})

    payload = _err(result := CliRunner().invoke(_make("probe", body), [], obj=_obj()))
    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert payload["error"]["details"] == {"field": "x"}


def test_require_yes(backend: _Backend) -> None:
    def body(cmd: CommandContext) -> dict[str, Any]:
        cmd.require_yes("Deleting requires --yes.", details={"deck": "D"})
        return {"deleted": True}

    payload = _err(result := CliRunner().invoke(_make("probe", body), [], obj=_obj()))
    assert result.exit_code == 2
    assert payload["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert payload["error"]["details"] == {"deck": "D", "hint": "Re-run with --yes."}

    assert _ok(CliRunner().invoke(_make("probe", body), [], obj=_obj(yes=True)))["data"] == {
        "deleted": True
    }


def test_require_ids(backend: _Backend) -> None:
    def body(cmd: CommandContext, card_id: int | None, query: str | None) -> dict[str, Any]:
        cmd.require_ids(card_id, query)
        return {"id": card_id, "query": query}

    cmd = _make("probe", id_or_query("card")(body))
    payload = _err(CliRunner().invoke(cmd, [], obj=_obj()))
    assert payload["error"]["message"] == "Provide --id or --query."
    assert _ok(CliRunner().invoke(cmd, ["--id", "3"], obj=_obj()))["data"] == {
        "id": 3, "query": None
    }
    assert _ok(CliRunner().invoke(cmd, ["--query", "is:due"], obj=_obj()))["data"] == {
        "id": None, "query": "is:due"
    }


@pytest.mark.parametrize(
    ("exc", "ankiconnect", "expected_code"),
    [
        (SearchParseError("bad", query="q", position=3), True, "INVALID_INPUT"),
        (SearchParseError("bad", query="q", position=3), False, "INVALID_INPUT"),
        (AnkiConnectAPIError("findCards", "boom"), True, "INVALID_INPUT"),
        # mutating commands leave an AnkiConnect failure to their own table
        (AnkiConnectAPIError("findCards", "boom"), False, "BACKEND_OPERATION_FAILED"),
    ],
)
def test_query_errors(backend: _Backend, exc, ankiconnect, expected_code) -> None:
    def body(cmd: CommandContext) -> dict[str, Any]:
        with cmd.query_errors("q", ankiconnect=ankiconnect):
            raise exc
        return {}

    table = {AnkiConnectAPIError: ("BACKEND_OPERATION_FAILED", 1)}
    payload = _err(CliRunner().invoke(_make("probe", body, errors=table), [], obj=_obj()))
    assert payload["error"]["code"] == expected_code
    if expected_code == "INVALID_INPUT":
        details = payload["error"]["details"]
        assert details["query"] == "q"
        assert ("position" in details) is isinstance(exc, SearchParseError)


def test_command_exit_from_inside_errors_block_is_not_rewrapped(backend: _Backend) -> None:
    def body(cmd: CommandContext) -> dict[str, Any]:
        with cmd.errors():
            raise cmd.fail("UNDO_EMPTY", "nothing", exit_code=2)

    table = {Exception: ("BACKEND_OPERATION_FAILED", 1)}
    payload = _err(result := CliRunner().invoke(_make("probe", body, errors=table), [], obj=_obj()))
    assert result.exit_code == 2
    assert payload["error"]["code"] == "UNDO_EMPTY"
    assert result.stderr.count('"ok": false') == 1


def test_register_flag_registers_with_dispatcher(monkeypatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(command_mod, "register_command", lambda n, c: seen.update({n: c}))

    def body(cmd: CommandContext) -> None: ...

    made = anki_command("registered:probe")(body)
    assert seen == {"registered:probe": made}


def test_backend_unavailable_beats_a_runtime_error_row(backend: _Backend, monkeypatch) -> None:
    """BackendFactoryError and NotImplementedError are RuntimeError subclasses. A
    command whose table maps RuntimeError (note:bulk) must still report them as
    BACKEND_UNAVAILABLE/7 — the old handlers listed that clause first."""

    def boom(obj: dict[str, Any]):
        raise BackendFactoryError("collection unsupported")

    monkeypatch.setattr(factory_mod, "backend_session_from_context", boom)

    def body(cmd: CommandContext) -> dict[str, Any]:
        with cmd.errors(details={"deck": "D"}):
            return cmd.backend.hello()

    table = {RuntimeError: ("BACKEND_OPERATION_FAILED", 1)}
    payload = _err(result := CliRunner().invoke(_make("probe", body, errors=table), [], obj=_obj()))
    assert result.exit_code == 7
    assert payload["error"]["code"] == "BACKEND_UNAVAILABLE"
    assert payload["error"]["details"] == {"backend": "direct"}


def test_a_command_may_override_backend_unavailable_by_naming_the_exact_type(
    backend: _Backend,
) -> None:
    def body(cmd: CommandContext) -> dict[str, Any]:
        raise NotImplementedError("deliberate")

    table = {NotImplementedError: ("UNSUPPORTED_BACKEND", 2)}
    result = CliRunner().invoke(_make("probe", body, errors=table), [], obj=_obj())
    assert result.exit_code == 2
    assert _err(result)["error"]["code"] == "UNSUPPORTED_BACKEND"
