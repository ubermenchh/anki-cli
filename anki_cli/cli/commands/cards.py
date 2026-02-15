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


@click.command("cards")
@click.option("--query", default="", help="Anki search query")
@click.pass_context
def cards_cmd(ctx: click.Context, query: str) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    try:
        with backend_session_from_context(obj) as backend:
            ids = backend.find_cards(query=query)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="cards", obj=obj, error=exc)

    formatter.emit_success(
        command="cards",
        data={"query": query, "count": len(ids), "ids": ids},
    )


@click.command("card")
@click.option("--id", "card_id", required=True, type=int, help="Card ID")
@click.pass_context
def card_cmd(ctx: click.Context, card_id: int) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    try:
        with backend_session_from_context(obj) as backend:
            card = backend.get_card(card_id)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="card", obj=obj, error=exc)
    except AnkiConnectAPIError as exc:
        formatter.emit_error(
            command="card",
            code="ENTITY_NOT_FOUND",
            message=str(exc),
            details={"id": card_id},
        )
        raise click.exceptions.Exit(4) from exc

    formatter.emit_success(command="card", data=card)


@click.command("card:suspend")
@click.option("--id", "card_id", type=int, default=None, help="Card ID")
@click.option("--query", default=None, help="Search query for target cards")
@click.pass_context
def card_suspend_cmd(ctx: click.Context, card_id: int | None, query: str | None) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    if card_id is None and not query:
        formatter.emit_error(
            command="card:suspend",
            code="INVALID_INPUT",
            message="Provide --id or --query.",
        )
        raise click.exceptions.Exit(2)

    try:
        with backend_session_from_context(obj) as backend:
            target_ids = [card_id] if card_id is not None else backend.find_cards(query=query or "")
            result = backend.suspend_cards(target_ids)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="card:suspend", obj=obj, error=exc)
    except AnkiConnectAPIError as exc:
        formatter.emit_error(
            command="card:suspend",
            code="BACKEND_OPERATION_FAILED",
            message=str(exc),
            details={"id": card_id, "query": query},
        )
        raise click.exceptions.Exit(1) from exc

    formatter.emit_success(command="card:suspend", data=result)


@click.command("card:unsuspend")
@click.option("--id", "card_id", type=int, default=None, help="Card ID")
@click.option("--query", default=None, help="Search query for target cards")
@click.pass_context
def card_unsuspend_cmd(ctx: click.Context, card_id: int | None, query: str | None) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    if card_id is None and not query:
        formatter.emit_error(
            command="card:unsuspend",
            code="INVALID_INPUT",
            message="Provide --id or --query.",
        )
        raise click.exceptions.Exit(2)

    try:
        with backend_session_from_context(obj) as backend:
            target_ids = [card_id] if card_id is not None else backend.find_cards(query=query or "")
            result = backend.unsuspend_cards(target_ids)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="card:unsuspend", obj=obj, error=exc)
    except AnkiConnectAPIError as exc:
        formatter.emit_error(
            command="card:unsuspend",
            code="BACKEND_OPERATION_FAILED",
            message=str(exc),
            details={"id": card_id, "query": query},
        )
        raise click.exceptions.Exit(1) from exc

    formatter.emit_success(command="card:unsuspend", data=result)


register_command("cards", cards_cmd)
register_command("card", card_cmd)
register_command("card:suspend", card_suspend_cmd)
register_command("card:unsuspend", card_unsuspend_cmd)