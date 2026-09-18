from __future__ import annotations

from typing import Any

import click

from anki_cli.backends.ankiconnect import AnkiConnectAPIError
from anki_cli.backends.factory import (
    BackendFactoryError,
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


DEFAULT_CARD_LIMIT = 1000


def _run_card_search(ctx: click.Context, *, command: str, query: str, limit: int) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)
    warnings: list[str] = []

    try:
        with backend_session_from_context(obj) as backend:
            card_ids = backend.find_cards(query)
            total = len(card_ids)
            if limit > 0 and total > limit:
                # Every card costs a get_card round trip; an unbounded run on a
                # large collection is the footgun `cards` without --query invites.
                card_ids = card_ids[:limit]
                warnings.append(
                    f"{total} cards matched; showing the first {limit}. "
                    "Pass --limit 0 for all, or use cards:ids for ids only."
                )
            cards: list[dict[str, Any]] = []
            for cid in card_ids:
                cards.append(backend.get_card(cid))
    except BackendFactoryError as exc:
        _emit_backend_unavailable(ctx=ctx, command=command, obj=obj, error=exc)
    except (SearchParseError, AnkiConnectAPIError) as exc:
        _emit_invalid_query(ctx=ctx, command=command, query=query, error=exc)

    formatter.emit_success(
        command=command,
        data={"query": query, "count": len(cards), "total": total, "items": cards},
        warnings=warnings,
    )


_LIMIT_OPTION = click.option(
    "--limit",
    type=int,
    default=DEFAULT_CARD_LIMIT,
    show_default=True,
    help="Max cards to return with details (0 = no limit); count of all matches is in `total`",
)


@click.command("cards")
@click.option("--query", default="", help="Anki search query (empty = every card)")
@_LIMIT_OPTION
@click.pass_context
def cards_cmd(ctx: click.Context, query: str, limit: int) -> None:
    """List cards matching a query, with full details.

    ``cards:ids`` returns ids only; ``browse`` opens the interactive TUI.
    """
    _run_card_search(ctx, command="cards", query=query, limit=limit)


@click.command("search", hidden=True)
@click.option("--query", required=True, help="Anki search query")
@_LIMIT_OPTION
@click.pass_context
def search_cmd(ctx: click.Context, query: str, limit: int) -> None:
    """Alias of ``cards`` (kept for existing scripts)."""
    _run_card_search(ctx, command="search", query=query, limit=limit)


register_command("cards", cards_cmd)
register_command("search", search_cmd)
