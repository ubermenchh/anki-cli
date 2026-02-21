from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

from click.testing import CliRunner

import anki_cli.cli.commands.notetype as nt_cmd_mod
from anki_cli.backends.factory import BackendFactoryError
from anki_cli.cli.commands.notetype import (
    notetype_cmd,
    notetype_create_cmd,
    notetype_css_cmd,
    notetype_field_add_cmd,
    notetype_field_remove_cmd,
    notetype_template_add_cmd,
    notetype_template_edit_cmd,
    notetypes_cmd,
)
from anki_cli.cli.dispatcher import get_command


def _base_obj(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "format": "json",
        "backend": "direct",
        "collection_path": None,
        "no_color": True,
        "copy": False,
    }
    base.update(overrides)
    return base


def _success_payload(result) -> dict[str, Any]:
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    return payload


def _error_payload(result) -> dict[str, Any]:
    assert result.exit_code != 0
    raw = (getattr(result, "stderr", "") or result.output).strip()
    payload = json.loads(raw)
    assert payload["ok"] is False
    return payload


def _patch_session(monkeypatch, backend: Any) -> None:
    @contextmanager
    def fake_session(obj: dict[str, Any]):
        yield backend

    monkeypatch.setattr(nt_cmd_mod, "backend_session_from_context", fake_session)


def test_notetypes_cmd_success(monkeypatch) -> None:
    class Backend:
        def get_notetypes(self) -> list[dict[str, Any]]:
            return [{"name": "Basic"}, {"name": "Cloze"}]

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(notetypes_cmd, [], obj=_base_obj())
    payload = _success_payload(result)

    assert payload["meta"]["command"] == "notetypes"
    assert payload["data"] == {
        "count": 2,
        "items": [{"name": "Basic"}, {"name": "Cloze"}],
    }


def test_notetypes_cmd_backend_unavailable_exit_7(monkeypatch) -> None:
    def failing_session(obj: dict[str, Any]):
        raise BackendFactoryError("backend down")

    monkeypatch.setattr(nt_cmd_mod, "backend_session_from_context", failing_session)

    runner = CliRunner()
    result = runner.invoke(notetypes_cmd, [], obj=_base_obj(backend="direct"))
    payload = _error_payload(result)

    assert result.exit_code == 7
    assert payload["error"]["code"] == "BACKEND_UNAVAILABLE"
    assert payload["error"]["details"] == {"backend": "direct"}


def test_notetype_cmd_success_trims_name(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class Backend:
        def get_notetype(self, name: str) -> dict[str, Any]:
            captured["name"] = name
            return {"name": name, "fields": ["Front", "Back"]}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(notetype_cmd, ["--notetype", "  Basic  "], obj=_base_obj())
    payload = _success_payload(result)

    assert payload["meta"]["command"] == "notetype"
    assert payload["data"] == {"name": "Basic", "fields": ["Front", "Back"]}
    assert captured["name"] == "Basic"


def test_notetype_cmd_not_found_exit_4(monkeypatch) -> None:
    class Backend:
        def get_notetype(self, name: str) -> dict[str, Any]:
            raise LookupError("missing")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(notetype_cmd, ["--name", "Basic"], obj=_base_obj())
    payload = _error_payload(result)

    assert result.exit_code == 4
    assert payload["error"]["code"] == "ENTITY_NOT_FOUND"
    assert payload["error"]["details"] == {"notetype": "Basic"}


def test_notetype_create_defaults_for_normal(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class Backend:
        def create_notetype(
            self,
            *,
            name: str,
            fields: list[str],
            templates: list[dict[str, str]],
            css: str = "",
            kind: str = "normal",
        ) -> dict[str, Any]:
            captured["name"] = name
            captured["fields"] = fields
            captured["templates"] = templates
            captured["css"] = css
            captured["kind"] = kind
            return {"name": name, "created": True}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(
        notetype_create_cmd,
        ["--name", "  NewType  "],
        obj=_base_obj(),
    )
    payload = _success_payload(result)

    assert payload["meta"]["command"] == "notetype:create"
    assert payload["data"] == {"name": "NewType", "created": True}
    assert captured["name"] == "NewType"
    assert captured["fields"] == ["Front", "Back"]
    assert captured["kind"] == "normal"
    assert captured["css"] == ""
    assert captured["templates"] == [
        {
            "name": "Card 1",
            "front": "{{Front}}",
            "back": "{{FrontSide}}\n\n<hr id=answer>\n\n{{Back}}",
        }
    ]


def test_notetype_create_defaults_for_cloze(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class Backend:
        def create_notetype(
            self,
            *,
            name: str,
            fields: list[str],
            templates: list[dict[str, str]],
            css: str = "",
            kind: str = "normal",
        ) -> dict[str, Any]:
            captured["name"] = name
            captured["fields"] = fields
            captured["templates"] = templates
            captured["kind"] = kind
            return {"name": name, "created": True}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(
        notetype_create_cmd,
        ["--name", "ClozeX", "--kind", "cloze"],
        obj=_base_obj(),
    )
    _ = _success_payload(result)

    assert captured["fields"] == ["Text", "Extra"]
    assert captured["kind"] == "cloze"
    assert captured["templates"] == [
        {"name": "Cloze", "front": "{{cloze:Text}}", "back": "{{cloze:Text}}\n\n{{Extra}}"}
    ]


def test_notetype_create_custom_values_and_fields_trim(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class Backend:
        def create_notetype(
            self,
            *,
            name: str,
            fields: list[str],
            templates: list[dict[str, str]],
            css: str = "",
            kind: str = "normal",
        ) -> dict[str, Any]:
            captured["name"] = name
            captured["fields"] = fields
            captured["templates"] = templates
            captured["css"] = css
            captured["kind"] = kind
            return {"name": name, "created": True}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(
        notetype_create_cmd,
        [
            "--name",
            "  MyType  ",
            "--kind",
            "NORMAL",
            "--field",
            "  Front  ",
            "--field",
            " Back ",
            "--template",
            " CardX ",
            "--front",
            "{{Front}}?",
            "--back",
            "{{Back}}!",
            "--css",
            ".card{}",
        ],
        obj=_base_obj(),
    )
    _ = _success_payload(result)

    assert captured == {
        "name": "MyType",
        "fields": ["Front", "Back"],
        "templates": [{"name": "CardX", "front": "{{Front}}?", "back": "{{Back}}!"}],
        "css": ".card{}",
        "kind": "normal",
    }


def test_notetype_create_backend_failure_exit_1(monkeypatch) -> None:
    class Backend:
        def create_notetype(self, **kwargs: Any) -> dict[str, Any]:
            raise ValueError("bad input")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(notetype_create_cmd, ["--name", "X"], obj=_base_obj())
    payload = _error_payload(result)

    assert result.exit_code == 1
    assert payload["error"]["code"] == "BACKEND_OPERATION_FAILED"
    assert payload["error"]["details"] == {"notetype": "X"}


def test_notetype_field_add_success(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class Backend:
        def add_notetype_field(self, name: str, field_name: str) -> dict[str, Any]:
            captured["name"] = name
            captured["field_name"] = field_name
            return {"name": name, "field": field_name, "added": True}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(
        notetype_field_add_cmd,
        ["--notetype", "  Basic  ", "--field", "  Extra  "],
        obj=_base_obj(),
    )
    payload = _success_payload(result)

    assert payload["data"] == {"name": "Basic", "field": "Extra", "added": True}
    assert captured == {"name": "Basic", "field_name": "Extra"}


def test_notetype_field_remove_success(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class Backend:
        def remove_notetype_field(self, name: str, field_name: str) -> dict[str, Any]:
            captured["name"] = name
            captured["field_name"] = field_name
            return {"name": name, "field": field_name, "removed": True}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(
        notetype_field_remove_cmd,
        ["--notetype", "Basic", "--field", "Extra"],
        obj=_base_obj(),
    )
    payload = _success_payload(result)

    assert payload["data"] == {"name": "Basic", "field": "Extra", "removed": True}
    assert captured == {"name": "Basic", "field_name": "Extra"}


def test_notetype_template_add_success(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class Backend:
        def add_notetype_template(
            self,
            name: str,
            template_name: str,
            front: str,
            back: str,
        ) -> dict[str, Any]:
            captured["args"] = (name, template_name, front, back)
            return {"name": name, "template": template_name, "added": True}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(
        notetype_template_add_cmd,
        ["--notetype", " Basic ", "--template", " Card 2 ", "--front", "Q", "--back", "A"],
        obj=_base_obj(),
    )
    payload = _success_payload(result)

    assert payload["data"] == {"name": "Basic", "template": "Card 2", "added": True}
    assert captured["args"] == ("Basic", "Card 2", "Q", "A")


def test_notetype_template_edit_requires_front_or_back_exit_2() -> None:
    runner = CliRunner()
    result = runner.invoke(
        notetype_template_edit_cmd,
        ["--notetype", "Basic", "--template", "Card 1"],
        obj=_base_obj(),
    )
    payload = _error_payload(result)

    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert "Provide at least one of --front or --back" in payload["error"]["message"]


def test_notetype_template_edit_success(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class Backend:
        def edit_notetype_template(
            self,
            name: str,
            template_name: str,
            *,
            front: str | None = None,
            back: str | None = None,
        ) -> dict[str, Any]:
            captured["name"] = name
            captured["template_name"] = template_name
            captured["front"] = front
            captured["back"] = back
            return {"name": name, "template": template_name, "updated": True}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(
        notetype_template_edit_cmd,
        ["--notetype", "Basic", "--template", "Card 1", "--front", "Q2"],
        obj=_base_obj(),
    )
    payload = _success_payload(result)

    assert payload["data"] == {"name": "Basic", "template": "Card 1", "updated": True}
    assert captured == {
        "name": "Basic",
        "template_name": "Card 1",
        "front": "Q2",
        "back": None,
    }


def test_notetype_css_get_reads_styling(monkeypatch) -> None:
    class Backend:
        def get_notetype(self, name: str) -> dict[str, Any]:
            assert name == "Basic"
            return {"name": name, "styling": {"css": ".card { color: red; }"}}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(notetype_css_cmd, ["--notetype", "Basic"], obj=_base_obj())
    payload = _success_payload(result)

    assert payload["data"] == {"name": "Basic", "css": ".card { color: red; }"}


def test_notetype_css_get_defaults_empty_css(monkeypatch) -> None:
    class Backend:
        def get_notetype(self, name: str) -> dict[str, Any]:
            return {"name": name, "styling": "not-a-dict"}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(notetype_css_cmd, ["--notetype", "Basic"], obj=_base_obj())
    payload = _success_payload(result)

    assert payload["data"] == {"name": "Basic", "css": ""}


def test_notetype_css_set_success(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class Backend:
        def set_notetype_css(self, name: str, css: str) -> dict[str, Any]:
            captured["name"] = name
            captured["css"] = css
            return {"name": name, "updated": True, "css": css}

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(
        notetype_css_cmd,
        ["--notetype", " Basic ", "--set", ".card{}"],
        obj=_base_obj(),
    )
    payload = _success_payload(result)

    assert payload["data"] == {"name": "Basic", "updated": True, "css": ".card{}"}
    assert captured == {"name": "Basic", "css": ".card{}"}


def test_notetype_css_backend_failure_exit_1(monkeypatch) -> None:
    class Backend:
        def get_notetype(self, name: str) -> dict[str, Any]:
            raise LookupError("missing")

    _patch_session(monkeypatch, Backend())

    runner = CliRunner()
    result = runner.invoke(notetype_css_cmd, ["--notetype", "Basic"], obj=_base_obj())
    payload = _error_payload(result)

    assert result.exit_code == 1
    assert payload["error"]["code"] == "BACKEND_OPERATION_FAILED"
    assert payload["error"]["details"] == {"notetype": "Basic"}


def test_notetype_commands_are_registered() -> None:
    assert get_command("notetypes") is not None
    assert get_command("notetype") is not None
    assert get_command("notetype:create") is not None
    assert get_command("notetype:field:add") is not None
    assert get_command("notetype:field:remove") is not None
    assert get_command("notetype:template:add") is not None
    assert get_command("notetype:template:edit") is not None
    assert get_command("notetype:css") is not None