from __future__ import annotations

from typing import Any

import click

from anki_cli.backends.ankiconnect import AnkiConnectAPIError
from anki_cli.backends.factory import (
    BackendFactoryError,
    BackendNotImplementedError,
    backend_session_from_context,
)
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


def _collect_note_ids(
    *,
    backend: Any,
    note_id: int | None,
    query: str | None,
) -> list[int]:
    if note_id is not None:
        return [note_id]
    return backend.find_notes(query=query or "")


def _normalize_tag_list(raw: str) -> list[str]:
    cleaned = raw.replace(",", " ").strip()
    return [part for part in cleaned.split(" ") if part]


@click.command("tags")
@click.pass_context
def tags_cmd(ctx: click.Context) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    try:
        with backend_session_from_context(obj) as backend:
            try:
                items = backend.get_tag_counts()
                formatter.emit_success(
                    command="tags",
                    data={
                        "count": len(items),
                        "items": sorted(items, key=lambda x: str(x["tag"]).lower()),
                    },
                )
                return
            except Exception:
                tags = backend.get_tags()
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="tags", obj=obj, error=exc)

    formatter.emit_success(
        command="tags",
        data={"count": len(tags), "items": sorted(tags, key=str.lower)},
    )


@click.command("tag")
@click.option("--tag", "tag_name", required=True, help="Tag name")
@click.pass_context
def tag_cmd(ctx: click.Context, tag_name: str) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    query = f'tag:"{tag_name.strip()}"'
    try:
        with backend_session_from_context(obj) as backend:
            note_ids = backend.find_notes(query=query)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="tag", obj=obj, error=exc)

    formatter.emit_success(
        command="tag",
        data={"tag": tag_name.strip(), "count": len(note_ids), "note_ids": note_ids},
    )


@click.command("tag:add")
@click.option("--id", "note_id", type=int, default=None, help="Single note ID")
@click.option("--query", default=None, help="Query selecting notes")
@click.option("--tag", "tag_name", required=True, help="Tag to add")
@click.pass_context
def tag_add_cmd(
    ctx: click.Context,
    note_id: int | None,
    query: str | None,
    tag_name: str,
) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    if note_id is None and not query:
        formatter.emit_error(
            command="tag:add",
            code="INVALID_INPUT",
            message="Provide --id or --query.",
        )
        raise click.exceptions.Exit(2)

    tags = _normalize_tag_list(tag_name)
    if not tags:
        formatter.emit_error(
            command="tag:add",
            code="INVALID_INPUT",
            message="Tag value cannot be empty.",
        )
        raise click.exceptions.Exit(2)

    try:
        with backend_session_from_context(obj) as backend:
            ids = _collect_note_ids(backend=backend, note_id=note_id, query=query)
            result = backend.add_tags(ids, tags)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="tag:add", obj=obj, error=exc)
    except AnkiConnectAPIError as exc:
        formatter.emit_error(
            command="tag:add",
            code="BACKEND_OPERATION_FAILED",
            message=str(exc),
        )
        raise click.exceptions.Exit(1) from exc

    formatter.emit_success(command="tag:add", data=result)


@click.command("tag:remove")
@click.option("--id", "note_id", type=int, default=None, help="Single note ID")
@click.option("--query", default=None, help="Query selecting notes")
@click.option("--tag", "tag_name", required=True, help="Tag to remove")
@click.pass_context
def tag_remove_cmd(
    ctx: click.Context,
    note_id: int | None,
    query: str | None,
    tag_name: str,
) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    if note_id is None and not query:
        formatter.emit_error(
            command="tag:remove",
            code="INVALID_INPUT",
            message="Provide --id or --query.",
        )
        raise click.exceptions.Exit(2)

    tags = _normalize_tag_list(tag_name)
    if not tags:
        formatter.emit_error(
            command="tag:remove",
            code="INVALID_INPUT",
            message="Tag value cannot be empty.",
        )
        raise click.exceptions.Exit(2)

    try:
        with backend_session_from_context(obj) as backend:
            ids = _collect_note_ids(backend=backend, note_id=note_id, query=query)
            result = backend.remove_tags(ids, tags)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="tag:remove", obj=obj, error=exc)
    except AnkiConnectAPIError as exc:
        formatter.emit_error(
            command="tag:remove",
            code="BACKEND_OPERATION_FAILED",
            message=str(exc),
        )
        raise click.exceptions.Exit(1) from exc

    formatter.emit_success(command="tag:remove", data=result)


@click.command("tag:rename")
@click.option("--from", "old_tag", required=True, help="Old tag")
@click.option("--to", "new_tag", required=True, help="New tag")
@click.pass_context
def tag_rename_cmd(ctx: click.Context, old_tag: str, new_tag: str) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    try:
        with backend_session_from_context(obj) as backend:
            result = backend.rename_tag(old_tag.strip(), new_tag.strip())
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="tag:rename", obj=obj, error=exc)
    except (AnkiConnectAPIError, LookupError, ValueError) as exc:
        formatter.emit_error(
            command="tag:rename",
            code="BACKEND_OPERATION_FAILED",
            message=str(exc),
            details={"from": old_tag, "to": new_tag},
        )
        raise click.exceptions.Exit(1) from exc

    formatter.emit_success(command="tag:rename", data=result)


register_command("tags", tags_cmd)
register_command("tag", tag_cmd)
register_command("tag:add", tag_add_cmd)
register_command("tag:remove", tag_remove_cmd)
register_command("tag:rename", tag_rename_cmd)
