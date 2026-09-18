from __future__ import annotations

from typing import Any

import click

from anki_cli.backends.ankiconnect import AnkiConnectAPIError
from anki_cli.cli.command import CommandContext, ErrorMap, anki_command, id_or_query
from anki_cli.core.render import extract_note_id, extract_ord, pick_template, render_card
from anki_cli.models.output import JSONValue

# Kept under their old private names: tests import them from here.
_extract_note_id = extract_note_id
_extract_ord = extract_ord
_pick_template = pick_template


def _collect_card_ids(*, backend: Any, card_id: int | None, query: str | None) -> list[int]:
    if card_id is not None:
        return [card_id]
    return backend.find_cards(query=query or "")


def _bounded_limit(value: int) -> int:
    return max(1, min(int(value), 1000))


@anki_command("cards:ids")
@click.option("--query", default="", help="Anki search query")
def cards_ids_cmd(cmd: CommandContext, query: str) -> JSONValue:
    """List card IDs matching a query."""
    with cmd.query_errors(query):
        ids = cmd.backend.find_cards(query=query)
    return {"query": query, "count": len(ids), "ids": ids}


_NOT_FOUND: ErrorMap = {
    AnkiConnectAPIError: ("ENTITY_NOT_FOUND", 4),
    LookupError: ("ENTITY_NOT_FOUND", 4),
}


@anki_command("card", errors=_NOT_FOUND)
@click.option("--id", "card_id", required=True, type=int, help="Card ID")
@click.option("--revlog-limit", default=10, type=int, show_default=True)
def card_cmd(cmd: CommandContext, card_id: int, revlog_limit: int) -> JSONValue:
    """Show detailed info for a single card."""
    backend = cmd.backend
    with cmd.errors(details={"id": card_id}):
        render = render_card(backend, card_id)
        # Revlog is optional: the AnkiConnect backend cannot read it.
        revlog: list[dict[str, Any]] | None
        try:
            revlog = backend.get_revlog(card_id=card_id, limit=_bounded_limit(revlog_limit))
        except NotImplementedError:
            revlog = None

    card_obj = render.card
    out: dict[str, Any] = dict(card_obj) if isinstance(card_obj, dict) else {"card": card_obj}
    out["rendered"] = render.rendered.as_dict() if render.rendered else None
    if render.error:
        out["render_error"] = render.error
    if revlog is not None:
        out["revlog"] = revlog
    return out


@anki_command("card:revlog", errors=_NOT_FOUND)
@click.option("--id", "card_id", required=True, type=int, help="Card ID")
@click.option("--limit", default=50, type=int, show_default=True, help="Max revlog rows (1..1000)")
def card_revlog_cmd(cmd: CommandContext, card_id: int, limit: int) -> JSONValue:
    """Show review history for a card."""
    bounded = _bounded_limit(limit)
    with cmd.errors(details={"id": card_id}):
        entries = cmd.backend.get_revlog(card_id=card_id, limit=bounded)
    return {"id": card_id, "limit": bounded, "count": len(entries), "items": entries}


# --- mutations by --id / --query -------------------------------------------------------

# suspend/unsuspend report an AnkiConnect failure with the target in details;
# the rest report any operation failure without details. Both are the
# pre-refactor contracts, pinned by tests.
_SUSPEND_ERRORS: ErrorMap = {AnkiConnectAPIError: ("BACKEND_OPERATION_FAILED", 1)}
_MUTATION_ERRORS: ErrorMap = {
    AnkiConnectAPIError: ("BACKEND_OPERATION_FAILED", 1),
    LookupError: ("BACKEND_OPERATION_FAILED", 1),
    ValueError: ("BACKEND_OPERATION_FAILED", 1),
}


def _target_ids(cmd: CommandContext, card_id: int | None, query: str | None) -> list[int]:
    cmd.require_ids(card_id, query)
    with cmd.query_errors(query, ankiconnect=False):
        return _collect_card_ids(backend=cmd.backend, card_id=card_id, query=query)


@anki_command("card:suspend", errors=_SUSPEND_ERRORS)
@id_or_query("card")
def card_suspend_cmd(cmd: CommandContext, card_id: int | None, query: str | None) -> JSONValue:
    """Suspend cards by ID or query."""
    # find_cards is inside the block too: an AnkiConnect failure while
    # resolving --query carries the same details it always did.
    with cmd.errors(details={"id": card_id, "query": query}):
        ids = _target_ids(cmd, card_id, query)
        return cmd.backend.suspend_cards(ids)


@anki_command("card:unsuspend", errors=_SUSPEND_ERRORS)
@id_or_query("card")
def card_unsuspend_cmd(cmd: CommandContext, card_id: int | None, query: str | None) -> JSONValue:
    """Unsuspend cards by ID or query."""
    with cmd.errors(details={"id": card_id, "query": query}):
        ids = _target_ids(cmd, card_id, query)
        return cmd.backend.unsuspend_cards(ids)


@anki_command("card:move", errors=_MUTATION_ERRORS)
@id_or_query("card")
@click.option("--deck", "deck_name", required=True, help="Destination deck name")
def card_move_cmd(
    cmd: CommandContext, card_id: int | None, query: str | None, deck_name: str
) -> JSONValue:
    """Move cards to a different deck."""
    ids = _target_ids(cmd, card_id, query)
    return cmd.backend.move_cards(ids, deck_name.strip())


@anki_command("card:flag", errors=_MUTATION_ERRORS)
@id_or_query("card")
@click.option("--flag", type=int, required=True, help="Flag value 0..7")
def card_flag_cmd(
    cmd: CommandContext, card_id: int | None, query: str | None, flag: int
) -> JSONValue:
    """Set a flag (0-7) on cards."""
    ids = _target_ids(cmd, card_id, query)
    return cmd.backend.set_card_flag(ids, flag)


@anki_command("card:bury", errors=_MUTATION_ERRORS)
@id_or_query("card")
def card_bury_cmd(cmd: CommandContext, card_id: int | None, query: str | None) -> JSONValue:
    """Bury cards until next session."""
    ids = _target_ids(cmd, card_id, query)
    return cmd.backend.bury_cards(ids)


@anki_command("card:unbury", errors=_MUTATION_ERRORS)
@click.option("--deck", "deck_name", default=None, help="Optional deck scope")
def card_unbury_cmd(cmd: CommandContext, deck_name: str | None) -> JSONValue:
    """Unbury all cards in a deck."""
    deck = deck_name.strip() if deck_name else None
    return cmd.backend.unbury_cards(deck=deck)


@anki_command("card:reschedule", errors=_MUTATION_ERRORS)
@id_or_query("card")
@click.option("--days", type=int, required=True, help="Days from today")
def card_reschedule_cmd(
    cmd: CommandContext, card_id: int | None, query: str | None, days: int
) -> JSONValue:
    """Reschedule cards to N days from today."""
    ids = _target_ids(cmd, card_id, query)
    return cmd.backend.reschedule_cards(ids, days)


@anki_command("card:reset", errors=_MUTATION_ERRORS)
@id_or_query("card")
def card_reset_cmd(cmd: CommandContext, card_id: int | None, query: str | None) -> JSONValue:
    """Reset cards to new (forget progress)."""
    ids = _target_ids(cmd, card_id, query)
    return cmd.backend.reset_cards(ids)

