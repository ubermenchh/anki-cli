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
from anki_cli.core.search import SearchParseError


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


def _emit_invalid_query(
    *,
    ctx: click.Context,
    command: str,
    query: str | None,
    error: Exception,
) -> None:
    formatter = formatter_from_ctx(ctx)
    details: dict[str, Any] = {"query": query or ""}
    if isinstance(error, SearchParseError) and error.position is not None:
        details["position"] = error.position

    formatter.emit_error(
        command=command,
        code="INVALID_INPUT",
        message=f"Invalid search query: {error}",
        details=details,
    )
    raise click.exceptions.Exit(2) from error


@click.command("search")
@click.option("--query", required=True, help="Anki search query")
@click.pass_context
def search_cmd(ctx: click.Context, query: str) -> None:
    """Search cards with full details."""
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    try:
        with backend_session_from_context(obj) as backend:
            card_ids = backend.find_cards(query)
            cards: list[dict[str, Any]] = []
            for cid in card_ids:
                cards.append(backend.get_card(cid))
    except (BackendNotImplementedError, BackendFactoryError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="search", obj=obj, error=exc)
    except (SearchParseError, AnkiConnectAPIError) as exc:
        _emit_invalid_query(ctx=ctx, command="search", query=query, error=exc)

    formatter.emit_success(
        command="search",
        data={"query": query, "count": len(cards), "items": cards},
    )


register_command("search", search_cmd)