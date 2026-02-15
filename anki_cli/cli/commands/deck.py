from __future__ import annotations

from typing import Any

import click

from anki_cli.backends.factory import (
    BackendFactoryError,
    BackendNotImplementedError,
    backend_session_from_context,
)
from anki_cli.cli.dispatcher import register_command
from anki_cli.cli.formatter import formatter_from_ctx


def _emit_backend_error(
    *,
    ctx: click.Context,
    command: str,
    obj: dict[str, Any],
    error: Exception,
    exit_code: int,
) -> None:
    formatter = formatter_from_ctx(ctx)
    formatter.emit_error(
        command=command,
        code="BACKEND_UNAVAILABLE",
        message=str(error),
        details={"backend": str(obj.get("backend", "unknown"))},
    )
    raise click.exceptions.Exit(exit_code) from error


@click.command("decks")
@click.pass_context
def decks_cmd(ctx: click.Context) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    try:
        with backend_session_from_context(obj) as backend:
            decks = backend.get_decks()
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_error(ctx=ctx, command="decks", obj=obj, error=exc, exit_code=7)

    formatter.emit_success(
        command="decks",
        data={"count": len(decks), "items": decks},
    )


@click.command("deck:create")
@click.option("--name", required=True, help="Deck name, e.g. Japanese::Vocab")
@click.pass_context
def deck_create_cmd(ctx: click.Context, name: str) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    if not name.strip():
        formatter.emit_error(
            command="deck:create",
            code="INVALID_INPUT",
            message="Deck name cannot be empty.",
        )
        raise click.exceptions.Exit(2)

    try:
        with backend_session_from_context(obj) as backend:
            result = backend.create_deck(name=name.strip())
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_error(ctx=ctx, command="deck:create", obj=obj, error=exc, exit_code=7)

    formatter.emit_success(command="deck:create", data=result)


@click.command("deck:delete")
@click.option("--deck", "deck_name", required=True, help="Deck name to delete")
@click.pass_context
def deck_delete_cmd(ctx: click.Context, deck_name: str) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    if not bool(obj.get("yes", False)):
        formatter.emit_error(
            command="deck:delete",
            code="CONFIRMATION_REQUIRED",
            message="Deleting a deck requires --yes.",
            details={"hint": "Run with --yes before the command."},
        )
        raise click.exceptions.Exit(2)

    try:
        with backend_session_from_context(obj) as backend:
            result = backend.delete_deck(name=deck_name.strip())
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_error(ctx=ctx, command="deck:delete", obj=obj, error=exc, exit_code=7)

    formatter.emit_success(command="deck:delete", data=result)


register_command("decks", decks_cmd)
register_command("deck:create", deck_create_cmd)
register_command("deck:delete", deck_delete_cmd)