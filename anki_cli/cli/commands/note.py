from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click

from anki_cli.backends.ankiconnect import AnkiConnectAPIError
from anki_cli.backends.factory import (
    BackendFactoryError,
    BackendNotImplementedError,
    backend_session_from_context,
)
from anki_cli.backends.protocol import JSONValue
from anki_cli.cli.dispatcher import register_command
from anki_cli.cli.formatter import formatter_from_ctx


def _emit_backend_unavailable(
    *,
    ctx: click.Context,
    command: str,
    obj: dict[str, Any],
    error: Exception,
) -> None:
    formatter = formatter_from_ctx(ctx)
    formatter.emit_error(
        command=command,
        code="BACKEND_UNAVAILABLE",
        message=str(error),
        details={"backend": str(obj.get("backend", "unknown"))},
    )
    raise click.exceptions.Exit(7) from error


def _parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    normalized = raw.replace(",", " ")
    tags = [part.strip() for part in normalized.split(" ") if part.strip()]
    return sorted(set(tags))


def _parse_dynamic_fields(extra_args: list[str]) -> dict[str, str]:
    """
    Parse unknown click args in the form:
      --Front Question --Back Answer
    """
    fields: dict[str, str] = {}
    i = 0

    while i < len(extra_args):
        token = extra_args[i]
        if not token.startswith("--"):
            raise click.ClickException(
                f"Unexpected field token '{token}'. Use --FieldName value."
            )

        key = token[2:].strip()
        if not key:
            raise click.ClickException("Empty field name is not allowed.")

        if i + 1 >= len(extra_args):
            raise click.ClickException(f"Missing value for field '{key}'.")

        value = extra_args[i + 1]
        if value.startswith("--"):
            raise click.ClickException(
                f"Missing value for field '{key}' before next option '{value}'."
            )

        fields[key] = value
        i += 2

    return fields


@click.command("notes")
@click.option("--query", default="", help="Anki search query")
@click.pass_context
def notes_cmd(ctx: click.Context, query: str) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    try:
        with backend_session_from_context(obj) as backend:
            ids = backend.find_notes(query=query)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="notes", obj=obj, error=exc)

    payload: dict[str, JSONValue] = {
        "query": query,
        "count": len(ids),
        "ids": ids,
    }
    formatter.emit_success(command="notes", data=payload)


@click.command("note")
@click.option("--id", "note_id", required=True, type=int, help="Note ID")
@click.pass_context
def note_cmd(ctx: click.Context, note_id: int) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    try:
        with backend_session_from_context(obj) as backend:
            note = backend.get_note(note_id)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="note", obj=obj, error=exc)
    except (AnkiConnectAPIError, LookupError) as exc:
        formatter.emit_error(
            command="note",
            code="ENTITY_NOT_FOUND",
            message=str(exc),
            details={"id": note_id},
        )
        raise click.exceptions.Exit(4) from exc

    formatter.emit_success(command="note", data=note)


@click.command(
    "note:add",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--deck", required=True, help="Deck name")
@click.option("--notetype", required=True, help="Notetype name")
@click.option("--tags", default="", help="Comma/space separated tags")
@click.pass_context
def note_add_cmd(
    ctx: click.Context,
    deck: str,
    notetype: str,
    tags: str,
) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    try:
        fields = _parse_dynamic_fields(list(ctx.args))
    except click.ClickException as exc:
        formatter.emit_error(
            command="note:add",
            code="INVALID_INPUT",
            message=str(exc),
        )
        raise click.exceptions.Exit(2) from exc

    if not fields:
        formatter.emit_error(
            command="note:add",
            code="INVALID_INPUT",
            message="No fields provided. Pass fields like Front=... Back=...",
        )
        raise click.exceptions.Exit(2)

    try:
        with backend_session_from_context(obj) as backend:
            note_id = backend.add_note(
                deck=deck.strip(),
                notetype=notetype.strip(),
                fields=fields,
                tags=_parse_tags(tags),
            )
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="note:add", obj=obj, error=exc)
    except (AnkiConnectAPIError, LookupError) as exc:
        formatter.emit_error(
            command="note:add",
            code="BACKEND_OPERATION_FAILED",
            message=str(exc),
            details={"deck": deck, "notetype": notetype},
        )
        raise click.exceptions.Exit(1) from exc

    payload: dict[str, JSONValue] = {
        "id": note_id,
        "deck": deck,
        "notetype": notetype,
        "fields": fields,
        "tags": _parse_tags(tags),
    }
    formatter.emit_success(command="note:add", data=payload)


@click.command(
    "note:edit",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--id", "note_id", required=True, type=int, help="Note ID")
@click.option("--tags", default=None, help="Replace tags with comma/space separated tags")
@click.pass_context
def note_edit_cmd(ctx: click.Context, note_id: int, tags: str | None) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    try:
        fields = _parse_dynamic_fields(list(ctx.args))
    except click.ClickException as exc:
        formatter.emit_error(
            command="note:edit",
            code="INVALID_INPUT",
            message=str(exc),
        )
        raise click.exceptions.Exit(2) from exc

    if not fields and tags is None:
        formatter.emit_error(
            command="note:edit",
            code="INVALID_INPUT",
            message="Nothing to update. Provide fields and/or tags.",
        )
        raise click.exceptions.Exit(2)

    parsed_tags = _parse_tags(tags) if tags is not None else None

    try:
        with backend_session_from_context(obj) as backend:
            result = backend.update_note(note_id=note_id, fields=fields or None, tags=parsed_tags)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="note:edit", obj=obj, error=exc)
    except (AnkiConnectAPIError, LookupError) as exc:
        formatter.emit_error(
            command="note:edit",
            code="BACKEND_OPERATION_FAILED",
            message=str(exc),
            details={"id": note_id},
        )
        raise click.exceptions.Exit(1) from exc

    formatter.emit_success(command="note:edit", data=result)


@click.command("note:delete")
@click.option("--id", "note_id", required=True, type=int, help="Note ID")
@click.pass_context
def note_delete_cmd(ctx: click.Context, note_id: int) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    if not bool(obj.get("yes", False)):
        formatter.emit_error(
            command="note:delete",
            code="CONFIRMATION_REQUIRED",
            message="Deleting a note requires --yes.",
            details={"hint": "Run with --yes before the command."},
        )
        raise click.exceptions.Exit(2)

    try:
        with backend_session_from_context(obj) as backend:
            result = backend.delete_notes([note_id])
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="note:delete", obj=obj, error=exc)
    except AnkiConnectAPIError as exc:
        formatter.emit_error(
            command="note:delete",
            code="BACKEND_OPERATION_FAILED",
            message=str(exc),
            details={"id": note_id},
        )
        raise click.exceptions.Exit(1) from exc

    formatter.emit_success(command="note:delete", data=result)


@click.command("note:bulk")
@click.option("--deck", required=True, help="Deck name")
@click.option("--notetype", required=True, help="Notetype name")
@click.option("--file", "file_path", type=click.Path(path_type=Path), default=None)
@click.pass_context
def note_bulk_cmd(
    ctx: click.Context,
    deck: str,
    notetype: str,
    file_path: Path | None,
) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    try:
        raw = file_path.read_text(encoding="utf-8") if file_path else sys.stdin.read()
        parsed = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        formatter.emit_error(
            command="note:bulk",
            code="INVALID_INPUT",
            message=f"Failed to read JSON input: {exc}",
        )
        raise click.exceptions.Exit(2) from exc

    if not isinstance(parsed, list):
        formatter.emit_error(
            command="note:bulk",
            code="INVALID_INPUT",
            message="Bulk input must be a JSON array of note objects.",
        )
        raise click.exceptions.Exit(2)

    notes_payload: list[dict[str, JSONValue]] = []
    for idx, item in enumerate(parsed):
        if not isinstance(item, dict):
            formatter.emit_error(
                command="note:bulk",
                code="INVALID_INPUT",
                message=f"Item {idx} is not an object.",
            )
            raise click.exceptions.Exit(2)

        fields = item.get("fields")
        if not isinstance(fields, dict):
            formatter.emit_error(
                command="note:bulk",
                code="INVALID_INPUT",
                message=f"Item {idx} is missing a 'fields' object.",
            )
            raise click.exceptions.Exit(2)

        tags = item.get("tags", [])
        notes_payload.append(
            {
                "deck": deck,
                "notetype": notetype,
                "fields": {str(k): str(v) for k, v in fields.items()},
                "tags": tags if isinstance(tags, list) else str(tags),
            }
        )

    try:
        with backend_session_from_context(obj) as backend:
            results = backend.add_notes(notes_payload)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="note:bulk", obj=obj, error=exc)
    except AnkiConnectAPIError as exc:
        formatter.emit_error(
            command="note:bulk",
            code="BACKEND_OPERATION_FAILED",
            message=str(exc),
            details={"deck": deck, "notetype": notetype},
        )
        raise click.exceptions.Exit(1) from exc

    success_count = len([item for item in results if item is not None])
    failed_count = len(results) - success_count

    payload: dict[str, JSONValue] = {
        "count": len(results),
        "created": success_count,
        "failed": failed_count,
        "ids": results,
    }
    formatter.emit_success(command="note:bulk", data=payload)


register_command("notes", notes_cmd)
register_command("note", note_cmd)
register_command("note:add", note_add_cmd)
register_command("note:edit", note_edit_cmd)
register_command("note:delete", note_delete_cmd)
register_command("note:bulk", note_bulk_cmd)