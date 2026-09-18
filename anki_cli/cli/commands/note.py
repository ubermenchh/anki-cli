from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click

from anki_cli.backends.ankiconnect import AnkiConnectAPIError
from anki_cli.backends.factory import backend_session_from_context  # noqa: F401  (patched by tests)
from anki_cli.cli.command import CommandContext, ErrorMap, anki_command
from anki_cli.cli.formatter import formatter_from_ctx  # noqa: F401  (patched by tests)
from anki_cli.db.anki_direct import DuplicateNoteError
from anki_cli.models.output import JSONValue

# Keys a note:bulk item may not use as field names: they read like AnkiConnect's
# per-note deck/notetype, which the CLI takes from its options instead.
_BULK_RESERVED_KEYS = frozenset({"deck", "deckName", "notetype", "modelName", "options"})


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
            raise click.ClickException(f"Unexpected field token '{token}'. Use --FieldName value.")

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



_DYNAMIC_FIELDS = {"ignore_unknown_options": True, "allow_extra_args": True}


def _dynamic_fields(cmd: CommandContext) -> dict[str, str]:
    try:
        return _parse_dynamic_fields(list(cmd.ctx.args))
    except click.ClickException as exc:
        raise cmd.invalid(str(exc)) from exc


@anki_command("notes")
@click.option("--query", default="", help="Anki search query")
def notes_cmd(cmd: CommandContext, query: str) -> JSONValue:
    """List note IDs matching a query."""
    with cmd.query_errors(query):
        ids = cmd.backend.find_notes(query=query)
    return {"query": query, "count": len(ids), "ids": ids}


@anki_command(
    "note",
    errors={AnkiConnectAPIError: ("ENTITY_NOT_FOUND", 4), LookupError: ("ENTITY_NOT_FOUND", 4)},
)
@click.option("--id", "note_id", required=True, type=int, help="Note ID")
def note_cmd(cmd: CommandContext, note_id: int) -> JSONValue:
    """Show detailed info for a single note."""
    with cmd.errors(details={"id": note_id}):
        return cmd.backend.get_note(note_id)


_OPERATION_FAILED: ErrorMap = {
    AnkiConnectAPIError: ("BACKEND_OPERATION_FAILED", 1),
    LookupError: ("BACKEND_OPERATION_FAILED", 1),
    ValueError: ("BACKEND_OPERATION_FAILED", 1),
}


def _add_note_message(exc: BaseException) -> str:
    # The store states the fact; the remedy flag is CLI surface, so it is
    # appended here. AnkiConnect reports duplicates as an AnkiConnectAPIError.
    if isinstance(exc, DuplicateNoteError):
        return f"{exc} Pass --allow-duplicate to add it anyway."
    return str(exc)


@anki_command("note:add", errors=_OPERATION_FAILED, context_settings=_DYNAMIC_FIELDS)
@click.option("--deck", required=True, help="Deck name")
@click.option("--notetype", required=True, help="Notetype name")
@click.option("--tags", default="", help="Comma/space separated tags")
@click.option("--allow-duplicate", is_flag=True, default=False, help="Allow duplicate notes")
def note_add_cmd(
    cmd: CommandContext, deck: str, notetype: str, tags: str, allow_duplicate: bool
) -> JSONValue:
    """Add a new note with custom fields."""
    fields = _dynamic_fields(cmd)
    if not fields:
        raise cmd.invalid("No fields provided. Pass fields like Front=... Back=...")

    def _details(exc: BaseException) -> dict[str, JSONValue]:
        details: dict[str, JSONValue] = {"deck": deck, "notetype": notetype}
        if isinstance(exc, DuplicateNoteError):
            details["duplicate_ids"] = list(exc.duplicate_ids)
        return details

    with cmd.errors(details=_details, message=_add_note_message):
        note_id = cmd.backend.add_note(
            deck=deck.strip(),
            notetype=notetype.strip(),
            fields=fields,
            tags=_parse_tags(tags),
            allow_duplicate=allow_duplicate,
        )
    return {
        "id": note_id,
        "deck": deck,
        "notetype": notetype,
        "fields": fields,
        "tags": _parse_tags(tags),
    }


@anki_command(
    "note:edit",
    errors={
        AnkiConnectAPIError: ("BACKEND_OPERATION_FAILED", 1),
        LookupError: ("BACKEND_OPERATION_FAILED", 1),
    },
    context_settings=_DYNAMIC_FIELDS,
)
@click.option("--id", "note_id", required=True, type=int, help="Note ID")
@click.option("--tags", default=None, help="Replace tags with comma/space separated tags")
def note_edit_cmd(cmd: CommandContext, note_id: int, tags: str | None) -> JSONValue:
    """Edit fields or tags on an existing note."""
    fields = _dynamic_fields(cmd)
    if not fields and tags is None:
        raise cmd.invalid("Nothing to update. Provide fields and/or tags.")
    parsed_tags = _parse_tags(tags) if tags is not None else None
    with cmd.errors(details={"id": note_id}):
        return cmd.backend.update_note(note_id=note_id, fields=fields or None, tags=parsed_tags)


@anki_command("note:delete", errors={AnkiConnectAPIError: ("BACKEND_OPERATION_FAILED", 1)})
@click.option("--id", "note_id", required=True, type=int, help="Note ID")
def note_delete_cmd(cmd: CommandContext, note_id: int) -> JSONValue:
    """Delete a note (requires --yes)."""
    cmd.require_yes("Deleting a note requires --yes.")
    with cmd.errors(details={"id": note_id}):
        return cmd.backend.delete_notes([note_id])


def _bulk_items(cmd: CommandContext, parsed: Any, deck: str, notetype: str) -> list[dict[str, Any]]:
    if not isinstance(parsed, list):
        raise cmd.invalid("Bulk input must be a JSON array of note objects.")

    notes_payload: list[dict[str, JSONValue]] = []
    for idx, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise cmd.invalid(f"Item {idx} is not an object.")

        # Two shapes are accepted: {"fields": {...}, "tags": [...]} and the flat
        # {"Front": "Q", "Back": "A", "tags": [...]} that SKILL.md documents.
        tags = item.get("tags") or []
        if not isinstance(tags, (list, str)):
            raise cmd.invalid(f"Item {idx}: 'tags' must be a list or a string.")
        reserved = _BULK_RESERVED_KEYS & item.keys()
        if reserved:
            # Per-item deck/notetype would be silently dropped (both backends
            # only read the notetype's own field names); refuse instead.
            raise cmd.invalid(
                f"Item {idx}: {sorted(reserved)} are not fields; the deck and "
                "notetype come from --deck / --notetype."
            )
        if "fields" in item:
            fields = item["fields"]
            if not isinstance(fields, dict):
                raise cmd.invalid(f"Item {idx}: 'fields' must be an object.")
        else:
            fields = {k: v for k, v in item.items() if k != "tags"}
            if not fields:
                raise cmd.invalid(
                    f"Item {idx} has no fields. Use {{\"Front\": ..., \"Back\": ...}} "
                    "or {\"fields\": {...}, \"tags\": [...]}."
                )

        notes_payload.append(
            {
                "deck": deck,
                "notetype": notetype,
                "fields": {str(k): str(v) for k, v in fields.items()},
                "tags": tags if isinstance(tags, list) else str(tags),
            }
        )
    return notes_payload


@anki_command(
    "note:bulk",
    errors={
        AnkiConnectAPIError: ("BACKEND_OPERATION_FAILED", 1),
        LookupError: ("BACKEND_OPERATION_FAILED", 1),
        ValueError: ("BACKEND_OPERATION_FAILED", 1),
        # Per-item refusals (duplicate/empty/missing deck) come back as null
        # ids; anything that would fail every item the same way (collection
        # locked, corrupt notetype config) propagates from add_notes and fails
        # the whole command rather than reporting N spurious per-item failures.
        RuntimeError: ("BACKEND_OPERATION_FAILED", 1),
    },
)
@click.option("--deck", required=True, help="Deck name")
@click.option("--notetype", required=True, help="Notetype name")
@click.option("--file", "file_path", type=click.Path(path_type=Path), default=None)
@click.option(
    "--allow-duplicate",
    is_flag=True,
    default=False,
    help="Add notes whose first field already exists in the notetype (otherwise they are null)",
)
def note_bulk_cmd(
    cmd: CommandContext, deck: str, notetype: str, file_path: Path | None, allow_duplicate: bool
) -> JSONValue:
    """Bulk-add notes from a JSON file or stdin."""
    try:
        raw = file_path.read_text(encoding="utf-8") if file_path else sys.stdin.read()
        parsed = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise cmd.invalid(f"Failed to read JSON input: {exc}") from exc

    notes_payload = _bulk_items(cmd, parsed, deck, notetype)
    with cmd.errors(details={"deck": deck, "notetype": notetype}):
        results = cmd.backend.add_notes(notes_payload, allow_duplicate=allow_duplicate)

    success_count = len([item for item in results if item is not None])
    return {
        "count": len(results),
        "created": success_count,
        "failed": len(results) - success_count,
        "ids": results,
    }


@anki_command("note:fields", errors=_OPERATION_FAILED)
@click.option("--id", "note_id", required=True, type=int, help="Note ID")
@click.option("--fields", default="", help="Comma-separated field names")
@click.option("--field", "field_list", multiple=True, help="Field name (repeatable)")
def note_fields_cmd(
    cmd: CommandContext, note_id: int, fields: str, field_list: tuple[str, ...]
) -> JSONValue:
    """Show field values for a note."""
    fields = ",".join([*field_list, fields]) if field_list else fields
    selected: list[str] | None = None
    if fields.strip():
        selected = [part.strip() for part in fields.split(",") if part.strip()]
    with cmd.errors(details={"id": note_id, "fields": selected or []}):
        values = cmd.backend.get_note_fields(note_id=note_id, fields=selected)
    return {"id": note_id, "fields": values}
