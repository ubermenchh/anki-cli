from __future__ import annotations

from typing import Any

import click

from anki_cli.backends.ankiconnect import AnkiConnectAPIError
from anki_cli.backends.factory import backend_session_from_context  # noqa: F401  (patched by tests)
from anki_cli.cli.command import CommandContext, anki_command, id_or_query
from anki_cli.cli.formatter import formatter_from_ctx  # noqa: F401  (patched by tests)
from anki_cli.models.output import JSONValue


def _collect_note_ids(*, backend: Any, note_id: int | None, query: str | None) -> list[int]:
    if note_id is not None:
        return [note_id]
    return backend.find_notes(query=query or "")


def _normalize_tag_list(raw: str) -> list[str]:
    cleaned = raw.replace(",", " ").strip()
    return [part for part in cleaned.split(" ") if part]


@anki_command("tags")
def tags_cmd(cmd: CommandContext) -> JSONValue:
    """List all tags with counts."""
    try:
        items = cmd.backend.get_tag_counts()
    except Exception:
        # Backends without counts fall back to the bare tag list.
        tags = cmd.backend.get_tags()
        return {"count": len(tags), "items": sorted(tags, key=str.lower)}
    return {"count": len(items), "items": sorted(items, key=lambda x: str(x["tag"]).lower())}


@anki_command("tag")
@click.option("--tag", "tag_name", required=True, help="Tag name")
def tag_cmd(cmd: CommandContext, tag_name: str) -> JSONValue:
    """Show notes associated with a tag."""
    query = f'tag:"{tag_name.strip()}"'
    with cmd.query_errors(query):
        note_ids = cmd.backend.find_notes(query=query)
    return {"tag": tag_name.strip(), "count": len(note_ids), "note_ids": note_ids}


def _tag_mutation(cmd: CommandContext, note_id: int | None, query: str | None, tag_name: str):
    cmd.require_ids(note_id, query)
    tags = _normalize_tag_list(tag_name)
    if not tags:
        raise cmd.invalid("Tag value cannot be empty.")
    with cmd.query_errors(query, ankiconnect=False):
        ids = _collect_note_ids(backend=cmd.backend, note_id=note_id, query=query)
    return ids, tags


_TAG_MUTATION_ERRORS = {AnkiConnectAPIError: ("BACKEND_OPERATION_FAILED", 1)}


@anki_command("tag:add", errors=_TAG_MUTATION_ERRORS)
@id_or_query("note")
@click.option("--tag", "--tags", "tag_name", required=True, help="Tag(s), space/comma separated")
def tag_add_cmd(cmd: CommandContext, note_id: int | None, query: str | None, tag_name: str):
    """Add a tag to notes by ID or query."""
    ids, tags = _tag_mutation(cmd, note_id, query, tag_name)
    return cmd.backend.add_tags(ids, tags)


@anki_command("tag:remove", errors=_TAG_MUTATION_ERRORS)
@id_or_query("note")
@click.option("--tag", "--tags", "tag_name", required=True, help="Tag(s), space/comma separated")
def tag_remove_cmd(cmd: CommandContext, note_id: int | None, query: str | None, tag_name: str):
    """Remove a tag from notes by ID or query."""
    ids, tags = _tag_mutation(cmd, note_id, query, tag_name)
    return cmd.backend.remove_tags(ids, tags)


@anki_command(
    "tag:rename",
    errors={
        AnkiConnectAPIError: ("BACKEND_OPERATION_FAILED", 1),
        LookupError: ("BACKEND_OPERATION_FAILED", 1),
        ValueError: ("BACKEND_OPERATION_FAILED", 1),
    },
)
@click.option("--from", "old_tag", required=True, help="Old tag")
@click.option("--to", "new_tag", required=True, help="New tag")
def tag_rename_cmd(cmd: CommandContext, old_tag: str, new_tag: str) -> JSONValue:
    """Rename a tag across all notes."""
    with cmd.errors(details={"from": old_tag, "to": new_tag}):
        return cmd.backend.rename_tag(old_tag.strip(), new_tag.strip())
