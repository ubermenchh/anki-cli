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


@click.command("notetypes")
@click.pass_context
def notetypes_cmd(ctx: click.Context) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    try:
        with backend_session_from_context(obj) as backend:
            items = backend.get_notetypes()
    except (BackendNotImplementedError, BackendFactoryError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="notetypes", obj=obj, error=exc)

    formatter.emit_success(
        command="notetypes",
        data={"count": len(items), "items": items},
    )


@click.command("notetype")
@click.option(
    "--notetype",
    "--name",
    "notetype_name",
    required=True,
    help="Notetype name, e.g. Basic",
)
@click.pass_context
def notetype_cmd(ctx: click.Context, notetype_name: str) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    normalized_name = notetype_name.strip()
    if not normalized_name:
        formatter.emit_error(
            command="notetype",
            code="INVALID_INPUT",
            message="Notetype name cannot be empty.",
            details={"notetype": notetype_name},
        )
        raise click.exceptions.Exit(2)

    try:
        with backend_session_from_context(obj) as backend:
            item = backend.get_notetype(normalized_name)
    except (BackendNotImplementedError, BackendFactoryError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="notetype", obj=obj, error=exc)
    except AnkiConnectAPIError as exc:
        formatter.emit_error(
            command="notetype",
            code="ENTITY_NOT_FOUND",
            message=str(exc),
            details={"notetype": normalized_name},
        )
        raise click.exceptions.Exit(4) from exc

    formatter.emit_success(command="notetype", data=item)


register_command("notetypes", notetypes_cmd)
register_command("notetype", notetype_cmd)