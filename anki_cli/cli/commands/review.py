from __future__ import annotations

from typing import Any

import click

from anki_cli.backends.ankiconnect import AnkiConnectAPIError, AnkiConnectProtocolError
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
        raise ValueError(
            "rating must be one of: 1,2,3,4,again,hard,good,easy"
        )
    return mapping[normalized]


@click.command("review")
@click.option("--deck", default=None, help="Optional deck filter")
@click.pass_context
def review_cmd(ctx: click.Context, deck: str | None) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    try:
        with backend_session_from_context(obj) as backend:
            counts = backend.get_due_counts(deck=deck.strip() if deck else None)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="review", obj=obj, error=exc)

    formatter.emit_success(
        command="review",
        data={
            "deck": deck,
            "due_counts": counts,
        },
    )


@click.command("review:answer")
@click.option("--id", "card_id", required=True, type=int, help="Card ID")
@click.option(
    "--rating",
    required=True,
    help="Rating: again|hard|good|easy or 1..4",
)
@click.pass_context
def review_answer_cmd(ctx: click.Context, card_id: int, rating: str) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    try:
        ease = _parse_ease(rating)
    except ValueError as exc:
        formatter.emit_error(
            command="review:answer",
            code="INVALID_INPUT",
            message=str(exc),
            details={"rating": rating},
        )
        raise click.exceptions.Exit(2) from exc

    try:
        with backend_session_from_context(obj) as backend:
            result = backend.answer_card(card_id=card_id, ease=ease)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="review:answer", obj=obj, error=exc)
    except (AnkiConnectAPIError, AnkiConnectProtocolError, LookupError) as exc:
        formatter.emit_error(
            command="review:answer",
            code="BACKEND_OPERATION_FAILED",
            message=str(exc),
            details={"id": card_id, "rating": rating, "ease": ease},
        )
        raise click.exceptions.Exit(1) from exc

    formatter.emit_success(command="review:answer", data=result)


register_command("review", review_cmd)
register_command("review:answer", review_answer_cmd)