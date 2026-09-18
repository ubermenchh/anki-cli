from __future__ import annotations

from typing import Any

import click

from anki_cli.backends.factory import backend_session_from_context  # noqa: F401  (patched by tests)
from anki_cli.cli.command import CommandContext, anki_command
from anki_cli.cli.formatter import formatter_from_ctx  # noqa: F401  (patched by tests)
from anki_cli.models.output import JSONValue

DEFAULT_CARD_LIMIT = 1000

_LIMIT_OPTION = click.option(
    "--limit",
    type=int,
    default=DEFAULT_CARD_LIMIT,
    show_default=True,
    help="Max cards to return with details (0 = no limit); count of all matches is in `total`",
)


def _card_search(cmd: CommandContext, *, query: str, limit: int) -> JSONValue:
    backend = cmd.backend
    with cmd.query_errors(query):
        card_ids = backend.find_cards(query)
    total = len(card_ids)
    if limit > 0 and total > limit:
        # Every card costs a get_card round trip; an unbounded run on a large
        # collection is the footgun `cards` without --query invites.
        card_ids = card_ids[:limit]
        cmd.warnings.append(
            f"{total} cards matched; showing the first {limit}. "
            "Pass --limit 0 for all, or use cards:ids for ids only."
        )
    cards: list[dict[str, Any]] = [backend.get_card(cid) for cid in card_ids]
    return {"query": query, "count": len(cards), "total": total, "items": cards}


@anki_command("cards")
@click.option("--query", default="", help="Anki search query (empty = every card)")
@_LIMIT_OPTION
def cards_cmd(cmd: CommandContext, query: str, limit: int) -> JSONValue:
    """List cards matching a query, with full details.

    ``cards:ids`` returns ids only; ``browse`` opens the interactive TUI.
    """
    return _card_search(cmd, query=query, limit=limit)


@anki_command("search", hidden=True)
@click.option("--query", required=True, help="Anki search query")
@_LIMIT_OPTION
def search_cmd(cmd: CommandContext, query: str, limit: int) -> JSONValue:
    """Alias of ``cards`` (kept for existing scripts)."""
    return _card_search(cmd, query=query, limit=limit)
