from __future__ import annotations

from typing import Any

import click

from anki_cli.cli.command import CommandContext, anki_command
from anki_cli.cli.paging import limit_option, offset_option, paginate
from anki_cli.models.output import JSONValue


def _card_search(cmd: CommandContext, *, query: str, limit: int, offset: int) -> JSONValue:
    backend = cmd.backend
    with cmd.query_errors(query):
        card_ids = backend.find_cards(query)
    total = len(card_ids)
    page = paginate(card_ids, limit=limit, offset=offset)
    if len(page) < total:
        # Details are fetched for the page only; an unbounded run on a large
        # collection is the footgun `cards` without --query invites.
        cmd.warnings.append(
            f"{total} cards matched; showing {len(page)} from offset {offset}. "
            "Raise --limit (0 = all), page with --offset, or use cards:ids for ids only."
        )
    cards: list[dict[str, Any]] = backend.get_cards(page) if page else []
    return {"query": query, "count": len(cards), "total": total, "items": cards}


@anki_command("cards")
@click.option("--query", default="", help="Anki search query (empty = every card)")
@limit_option
@offset_option
def cards_cmd(cmd: CommandContext, query: str, limit: int, offset: int) -> JSONValue:
    """List cards matching a query, with full details.

    ``cards:ids`` returns ids only; ``browse`` opens the interactive TUI.
    """
    return _card_search(cmd, query=query, limit=limit, offset=offset)


@anki_command("search", hidden=True)
@click.option("--query", required=True, help="Anki search query")
@limit_option
@offset_option
def search_cmd(cmd: CommandContext, query: str, limit: int, offset: int) -> JSONValue:
    """Alias of ``cards`` (kept for existing scripts)."""
    return _card_search(cmd, query=query, limit=limit, offset=offset)
