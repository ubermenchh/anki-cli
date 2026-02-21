import json
import sys
from pathlib import Path

import click
from pydantic import BaseModel

from anki_cli.cli.formatter import OutputFormatter, formatter_from_ctx


def _formatter(output_format: str, *, copy_output: bool = False) -> OutputFormatter:
    return OutputFormatter(
        output_format=output_format,
        backend="direct",
        collection_path="/tmp/collection.db",
        no_color=True,
        copy_output=copy_output,
    )


class DemoModel(BaseModel):
    value: int


def test_emit_success_json_structure(capsys) -> None:
    formatter = _formatter("json")
    formatter.emit_success(command="status", data={"ok_count": 3})

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert payload["ok"] is True
    assert payload["data"] == {"ok_count": 3}
    assert payload["meta"]["command"] == "status"
    assert payload["meta"]["backend"] == "direct"
    assert payload["meta"]["collection"] == "/tmp/collection.db"
    assert payload["meta"]["timestamp"].endswith("Z")
    assert captured.err == ""


def test_emit_success_accepts_pydantic_model(capsys) -> None:
    formatter = _formatter("json")
    formatter.emit_success(command="demo", data=DemoModel(value=7))

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert payload["ok"] is True
    assert payload["data"] == {"value": 7}


def test_emit_error_json_structure(capsys) -> None:
    formatter = _formatter("json")
    formatter.emit_error(
        command="deck",
        code="ENTITY_NOT_FOUND",
        message="Deck not found",
        details={"deck": "Missing"},
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.err)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "ENTITY_NOT_FOUND"
    assert payload["error"]["message"] == "Deck not found"
    assert payload["error"]["details"] == {"deck": "Missing"}
    assert payload["meta"]["command"] == "deck"
    assert captured.out == ""


def test_emit_error_plain_with_details(capsys) -> None:
    formatter = _formatter("plain")
    formatter.emit_error(
        command="note:add",
        code="INVALID_INPUT",
        message="Bad note",
        details={"fields": ["Front", "Back"]},
    )

    captured = capsys.readouterr()
    assert "INVALID_INPUT: Bad note" in captured.err
    assert "- fields: [\"Front\", \"Back\"]" in captured.err


def test_emit_success_md_items_table(capsys) -> None:
    formatter = _formatter("md")
    formatter.emit_success(
        command="decks",
        data={"items": [{"id": 1, "name": "Default"}, {"id": 2, "name": "Japanese"}]},
    )

    captured = capsys.readouterr()
    assert "| id | name |" in captured.out
    assert "| 1 | Default |" in captured.out
    assert "| 2 | Japanese |" in captured.out


def test_emit_success_csv_rows(capsys) -> None:
    formatter = _formatter("csv")
    formatter.emit_success(
        command="rows",
        data=[{"id": 1, "name": "A"}, {"id": 2, "name": "B"}],
    )

    captured = capsys.readouterr()
    assert captured.out.strip().splitlines() == ["id,name", "1,A", "2,B"]


def test_emit_success_plain_single_row(capsys) -> None:
    formatter = _formatter("plain")
    formatter.emit_success(command="one", data={"id": 1, "name": "Deck"})

    captured = capsys.readouterr()
    assert captured.out.strip() == "id=1\nname=Deck"


def test_emit_success_table_omits_nested_only_columns(capsys) -> None:
    formatter = _formatter("table")
    formatter.emit_success(
        command="table",
        data=[{"id": 1, "meta": {"a": 1}}],
    )

    captured = capsys.readouterr()
    text = captured.out.lower()
    assert "id" in text
    assert "1" in text
    assert "meta" not in text


def test_copy_output_uses_pyperclip(monkeypatch, capsys) -> None:
    class FakePyperclip:
        copied = ""

        @staticmethod
        def copy(text: str) -> None:
            FakePyperclip.copied = text

    monkeypatch.setitem(sys.modules, "pyperclip", FakePyperclip)

    formatter = _formatter("json", copy_output=True)
    formatter.emit_success(command="copy", data={"value": 1})
    _ = capsys.readouterr()

    assert '"ok": true' in FakePyperclip.copied
    assert '"value": 1' in FakePyperclip.copied


def test_formatter_from_ctx_coerces_path_values() -> None:
    ctx = click.Context(
        click.Command("dummy"),
        obj={
            "format": "json",
            "backend": "direct",
            "collection_path": Path("/tmp/test.db"),
            "no_color": True,
            "copy": False,
        },
    )

    formatter = formatter_from_ctx(ctx)

    assert formatter.output_format == "json"
    assert formatter.backend == "direct"
    assert formatter.collection_path == "/tmp/test.db"
    assert formatter.no_color is True
    assert formatter.copy_output is False