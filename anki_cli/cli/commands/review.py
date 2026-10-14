from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import click

from anki_cli.backends.ankiconnect import AnkiConnectAPIError, AnkiConnectProtocolError
from anki_cli.backends.protocol import BackendUnsupportedError
from anki_cli.cli.command import CommandContext, ErrorMap, anki_command
from anki_cli.core.render import extract_note_id, extract_ord, pick_template, render_card
from anki_cli.core.review_session import ReviewSession, parse_ease
from anki_cli.core.undo import UndoStore
from anki_cli.models.output import JSONValue

# Kept under their old private names: tests import them from here.
_extract_note_id = extract_note_id
_extract_ord = extract_ord


def _pick_template(templates: Mapping[str, Any], ord_: int) -> Mapping[str, Any] | None:
    picked = pick_template(templates, ord_)
    return picked[1] if picked is not None else None


def _render_card(*, backend: Any, card_id: int, reveal_answer: bool) -> dict[str, Any]:
    """``{"card", "rendered", "render_error"}`` for ``card_id`` (tests patch this)."""
    result = render_card(backend, card_id)
    rendered = result.rendered.as_dict(reveal_answer=reveal_answer) if result.rendered else None
    return {"card": result.card, "rendered": rendered, "render_error": result.error}


# Kept under the old private name: tests import it from here.
_parse_ease = parse_ease


def _session(backend: Any, deck: str | None = None) -> ReviewSession:
    """The shared review session (#36); the undo store is created here so the
    CLI's undo file location stays patchable in one place."""
    return ReviewSession(backend, deck, undo_store=UndoStore())


_OPERATION_FAILED: ErrorMap = {
    AnkiConnectAPIError: ("BACKEND_OPERATION_FAILED", 1),
    AnkiConnectProtocolError: ("BACKEND_OPERATION_FAILED", 1),
    LookupError: ("BACKEND_OPERATION_FAILED", 1),
    ValueError: ("BACKEND_OPERATION_FAILED", 1),
}


@anki_command("review")
@click.option("--deck", default=None, help="Optional deck filter")
def review_cmd(cmd: CommandContext, deck: str | None) -> JSONValue:
    """Show due counts for review."""
    counts = cmd.backend.get_due_counts(deck=deck.strip() if deck else None)
    return {"deck": deck, "due_counts": counts}


@anki_command("review:next", errors=_OPERATION_FAILED)
@click.option("--deck", default=None, help="Optional deck filter")
def review_next_cmd(cmd: CommandContext, deck: str | None) -> JSONValue:
    """Fetch the next due card (question only)."""
    with cmd.errors(details={"deck": deck}):
        backend = cmd.backend
        picked = _session(backend, deck).next()
        card_id, kind = picked.card_id, picked.kind

        if card_id is None:
            return {"deck": deck, "card_id": None, "kind": kind}

        rendered = _render_card(backend=backend, card_id=card_id, reveal_answer=False)
    return {
        "deck": deck,
        "kind": kind,
        "card_id": card_id,
        "question": (rendered.get("rendered") or {}).get("question"),
        "rendered": rendered.get("rendered"),
    }


@anki_command("review:show", errors=_OPERATION_FAILED)
@click.option("--deck", default=None, help="Optional deck filter")
def review_show_cmd(cmd: CommandContext, deck: str | None) -> JSONValue:
    """Show the next card with its answer."""
    with cmd.errors(details={"deck": deck}):
        backend = cmd.backend
        picked = _session(backend, deck).next()
        card_id, kind = picked.card_id, picked.kind
        if card_id is None:
            return {"deck": deck, "card_id": None, "kind": kind}
        rendered = _render_card(backend=backend, card_id=card_id, reveal_answer=True)
    return {"deck": deck, "kind": kind, "card_id": card_id, "rendered": rendered.get("rendered")}


@anki_command("review:preview", errors=_OPERATION_FAILED)
@click.option("--id", "card_id", required=True, type=int, help="Card ID")
def review_preview_cmd(cmd: CommandContext, card_id: int) -> JSONValue:
    """Preview scheduling outcome per rating."""
    backend = cmd.backend
    session = _session(backend)
    if not session.introspects:
        # Name the command, not the Protocol method, in the user's error.
        raise BackendUnsupportedError("review:preview", backend.name, "Use --backend direct.")
    with cmd.errors(details={"id": card_id}):
        items = session.preview(card_id)
    return {"card_id": card_id, "items": items}


@anki_command(
    "review:undo",
    errors={
        LookupError: ("BACKEND_OPERATION_FAILED", 1),
        ValueError: ("BACKEND_OPERATION_FAILED", 1),
    },
)
def review_undo_cmd(cmd: CommandContext) -> JSONValue:
    """Undo the last review answer."""
    backend = cmd.backend
    session = _session(backend)
    if not session.introspects:
        raise BackendUnsupportedError("review:undo", backend.name, "Use --backend direct.")

    outcome = session.undo()
    if outcome is None:
        raise cmd.fail("UNDO_EMPTY", "No undo entries available.", exit_code=2)
    return outcome.result


@anki_command(
    "review:answer",
    errors={
        AnkiConnectAPIError: ("BACKEND_OPERATION_FAILED", 1),
        AnkiConnectProtocolError: ("BACKEND_OPERATION_FAILED", 1),
        LookupError: ("BACKEND_OPERATION_FAILED", 1),
    },
)
@click.option("--id", "card_id", required=True, type=int, help="Card ID")
@click.option("--rating", required=True, help="Rating: again|hard|good|easy or 1..4")
def review_answer_cmd(cmd: CommandContext, card_id: int, rating: str) -> JSONValue:
    """Answer a card (again/hard/good/easy)."""
    try:
        ease = _parse_ease(rating)
    except ValueError as exc:
        raise cmd.invalid(str(exc), details={"rating": rating}) from exc

    with cmd.errors(details={"id": card_id, "rating": rating, "ease": ease}):
        outcome = _session(cmd.backend).answer(card_id, ease)
        if outcome.undo_error:
            # The answer is committed; failing the command now would invite a
            # retry that applies a second review.
            cmd.warnings.append(f"answer saved but undo entry not written: {outcome.undo_error}")
    return outcome.result


TUI_HINT = {"hint": "Run: uv sync --extra tui"}


@anki_command("review:start")
@click.option("--deck", default=None, help="Optional deck filter")
def review_start_cmd(cmd: CommandContext, deck: str | None) -> None:
    """Start an interactive review session (TUI)."""
    try:
        from anki_cli.tui.review_app import ReviewApp
    except Exception as exc:
        raise cmd.fail(
            "TUI_NOT_AVAILABLE",
            f"Textual is not installed/available: {exc}",
            exit_code=2,
            details=TUI_HINT,
        ) from exc

    backend = cmd.backend
    if not _session(backend).introspects:
        raise cmd.fail(
            "UNSUPPORTED_BACKEND",
            "review:start currently supports only direct backend.",
            exit_code=2,
            details={"backend": getattr(backend, "name", "unknown")},
        )
    cmd.run_tui(
        ReviewApp(backend=backend, deck=deck.strip() if deck else None, **cmd.tui_options())
    )
    return None
