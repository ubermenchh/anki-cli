from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import click

from anki_cli.backends.ankiconnect import AnkiConnectAPIError, AnkiConnectProtocolError
from anki_cli.backends.protocol import BackendUnsupportedError
from anki_cli.cli.command import CommandContext, ErrorMap, anki_command
from anki_cli.core.render import extract_note_id, extract_ord, pick_template, render_card
from anki_cli.core.scheduler import pick_next_due_card_id
from anki_cli.core.undo import UndoItem, UndoStore, now_epoch_ms
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


def _parse_ease(rating: str) -> int:
    normalized = rating.strip().lower()
    mapping = {
        "1": 1,
        "2": 2,
        "3": 3,
        "4": 4,
        "again": 1,
        "hard": 2,
        "good": 3,
        "easy": 4,
    }
    if normalized not in mapping:
        raise ValueError("rating must be one of: 1,2,3,4,again,hard,good,easy")
    return mapping[normalized]


def _introspects(backend: Any) -> bool:
    return bool(getattr(backend, "supports_scheduler_introspection", False))


def _collection_of(backend: Any) -> str:
    col = getattr(backend, "collection_path", None)
    return str(col) if col is not None else ""


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
        card_id: int | None
        kind: str
        if _introspects(backend):
            # The scheduler's own ordering; the query-based picker is the
            # approximation for backends that cannot expose it.
            picked = backend.get_next_due_card(deck.strip() if deck else None)
            card_id = picked.get("card_id") if isinstance(picked, dict) else None
            kind = str(picked.get("kind", "none")) if isinstance(picked, dict) else "none"
        else:
            card_id, kind = pick_next_due_card_id(backend, deck=deck)

        if not isinstance(card_id, int) or card_id <= 0:
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
        card_id, kind = pick_next_due_card_id(backend, deck=deck)
        if card_id is None:
            return {"deck": deck, "card_id": None, "kind": kind}
        rendered = _render_card(backend=backend, card_id=card_id, reveal_answer=True)
    return {"deck": deck, "kind": kind, "card_id": card_id, "rendered": rendered.get("rendered")}


@anki_command("review:preview", errors=_OPERATION_FAILED)
@click.option("--id", "card_id", required=True, type=int, help="Card ID")
def review_preview_cmd(cmd: CommandContext, card_id: int) -> JSONValue:
    """Preview scheduling outcome per rating."""
    backend = cmd.backend
    if not _introspects(backend):
        # Name the command, not the Protocol method, in the user's error.
        raise BackendUnsupportedError("review:preview", backend.name, "Use --backend direct.")
    with cmd.errors(details={"id": card_id}):
        items = backend.preview_ratings(int(card_id))
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
    if not _introspects(backend):
        raise BackendUnsupportedError("review:undo", backend.name, "Use --backend direct.")

    item = UndoStore().pop(collection=_collection_of(backend))
    if item is None:
        raise cmd.fail("UNDO_EMPTY", "No undo entries available.", exit_code=2)
    return backend.restore_card_state(item.snapshot)


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
        backend = cmd.backend
        # Save undo snapshot (direct backend only). The snapshot must capture
        # pre-answer state, but it is pushed only after answer_card succeeds so
        # a failed answer cannot leave a stale undo entry.
        snapshot: dict[str, Any] | None = None
        if _introspects(backend):
            snapshot = cast(dict[str, Any], backend.snapshot_card_state(int(card_id)))

        result = backend.answer_card(card_id=int(card_id), ease=ease)

        if snapshot is not None:
            # Carry the id of the revlog row this answer wrote so undo can
            # delete exactly that row instead of a time window.
            revlog_id = result.get("revlog_id") if isinstance(result, Mapping) else None
            undo_item = UndoItem(
                collection=_collection_of(backend),
                card_id=int(card_id),
                snapshot={**snapshot, "revlog_id": revlog_id},
                created_at_epoch_ms=now_epoch_ms(),
            )
            # Best-effort: the answer is already committed, so a failed undo
            # write must not fail the command (a retry would apply a second
            # review).
            try:
                UndoStore().push(undo_item)
            except OSError as exc:
                cmd.warnings.append(f"answer saved but undo entry not written: {exc}")
    return result


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
    if not _introspects(backend):
        raise cmd.fail(
            "UNSUPPORTED_BACKEND",
            "review:start currently supports only direct backend.",
            exit_code=2,
            details={"backend": getattr(backend, "name", "unknown")},
        )
    ReviewApp(backend=backend, deck=deck.strip() if deck else None).run()
    return None
