from __future__ import annotations

from typing import Any

import click

from anki_cli.backends.factory import (
    BackendFactoryError,
    BackendNotImplementedError,
    backend_session_from_context,
)
from anki_cli.backends.protocol import JSONValue
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


def _deck_chain(name: str) -> list[str]:
    raw = name.strip()
    if not raw:
        raise ValueError("Deck name cannot be empty.")

    parts = [part.strip() for part in raw.split("::")]
    if any(not part for part in parts):
        raise ValueError("Deck hierarchy has empty segment(s). Use names like A::B::C.")

    chain: list[str] = []
    for idx in range(1, len(parts) + 1):
        chain.append("::".join(parts[:idx]))
    return chain


def _parse_step_values(raw: str | None) -> list[float] | None:
    if raw is None:
        return None
    cleaned = [part.strip() for part in raw.split(",") if part.strip()]
    if not cleaned:
        return []
    return [float(value) for value in cleaned]


@click.command("decks")
@click.pass_context
def decks_cmd(ctx: click.Context) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)
    table_mode = str(obj.get("format", "table")).lower() == "table"

    try:
        with backend_session_from_context(obj) as backend:
            decks = backend.get_decks()
            items: list[dict[str, JSONValue]] = []
            for deck in decks:
                deck_name = str(deck.get("name", ""))
                due = backend.get_due_counts(deck=deck_name)
                parts = [part for part in deck_name.split("::") if part]
                level = max(0, len(parts) - 1)
                leaf = parts[-1] if parts else deck_name

                item: dict[str, JSONValue] = {
                    **deck,
                    "new": due.get("new", 0),
                    "learn": due.get("learn", 0),
                    "review": due.get("review", 0),
                    "total_due": due.get("total", 0),
                    "level": level,
                }
                if table_mode:
                    item["name"] = f"{'  ' * level}{leaf}"
                items.append(item)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_error(ctx=ctx, command="decks", obj=obj, error=exc, exit_code=7)

    formatter.emit_success(
        command="decks",
        data={"count": len(items), "items": items},
    )


@click.command("deck")
@click.option("--deck", "deck_name", required=True, help="Deck name")
@click.pass_context
def deck_cmd(ctx: click.Context, deck_name: str) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)
    normalized = deck_name.strip()

    if not normalized:
        formatter.emit_error(
            command="deck",
            code="INVALID_INPUT",
            message="Deck name cannot be empty.",
        )
        raise click.exceptions.Exit(2)

    try:
        with backend_session_from_context(obj) as backend:
            deck = backend.get_deck(normalized)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_error(ctx=ctx, command="deck", obj=obj, error=exc, exit_code=7)
    except LookupError as exc:
        formatter.emit_error(
            command="deck",
            code="ENTITY_NOT_FOUND",
            message=str(exc),
            details={"deck": normalized},
        )
        raise click.exceptions.Exit(4) from exc

    formatter.emit_success(
        command="deck",
        data=deck,
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
        chain = _deck_chain(name)
    except ValueError as exc:
        formatter.emit_error(
            command="deck:create",
            code="INVALID_INPUT",
            message=str(exc),
        )
        raise click.exceptions.Exit(2) from exc

    created: list[dict[str, JSONValue]] = []
    existing: list[dict[str, JSONValue]] = []
    try:
        with backend_session_from_context(obj) as backend:
            for item in chain:
                result = backend.create_deck(name=item)
                if bool(result.get("created", True)):
                    created.append(result)
                else:
                    existing.append(result)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_error(ctx=ctx, command="deck:create", obj=obj, error=exc, exit_code=7)

    formatter.emit_success(
        command="deck:create",
        data={
            "requested": name.strip(),
            "chain": chain,
            "created_count": len(created),
            "existing_count": len(existing),
            "created": created,
            "existing": existing,
        },
    )


@click.command("deck:rename")
@click.option("--from", "from_name", required=True, help="Current deck name")
@click.option("--to", "to_name", required=True, help="New deck name")
@click.pass_context
def deck_rename_cmd(ctx: click.Context, from_name: str, to_name: str) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)
    source = from_name.strip()
    target = to_name.strip()

    if not source or not target:
        formatter.emit_error(
            command="deck:rename",
            code="INVALID_INPUT",
            message="Both --from and --to are required.",
        )
        raise click.exceptions.Exit(2)

    try:
        _deck_chain(target)
    except ValueError as exc:
        formatter.emit_error(
            command="deck:rename",
            code="INVALID_INPUT",
            message=str(exc),
        )
        raise click.exceptions.Exit(2) from exc

    try:
        with backend_session_from_context(obj) as backend:
            result = backend.rename_deck(old_name=source, new_name=target)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_error(ctx=ctx, command="deck:rename", obj=obj, error=exc, exit_code=7)
    except LookupError as exc:
        formatter.emit_error(
            command="deck:rename",
            code="ENTITY_NOT_FOUND",
            message=str(exc),
            details={"from": source, "to": target},
        )
        raise click.exceptions.Exit(4) from exc
    except ValueError as exc:
        formatter.emit_error(
            command="deck:rename",
            code="INVALID_INPUT",
            message=str(exc),
            details={"from": source, "to": target},
        )
        raise click.exceptions.Exit(2) from exc

    formatter.emit_success(command="deck:rename", data=result)


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


@click.command("deck:config")
@click.option("--deck", "deck_name", required=True, help="Deck name")
@click.pass_context
def deck_config_cmd(ctx: click.Context, deck_name: str) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)
    normalized = deck_name.strip()

    if not normalized:
        formatter.emit_error(
            command="deck:config",
            code="INVALID_INPUT",
            message="Deck name cannot be empty.",
        )
        raise click.exceptions.Exit(2)

    try:
        with backend_session_from_context(obj) as backend:
            data = backend.get_deck_config(normalized)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_error(ctx=ctx, command="deck:config", obj=obj, error=exc, exit_code=7)
    except (LookupError, ValueError) as exc:
        formatter.emit_error(
            command="deck:config",
            code="ENTITY_NOT_FOUND",
            message=str(exc),
            details={"deck": normalized},
        )
        raise click.exceptions.Exit(4) from exc

    formatter.emit_success(command="deck:config", data=data)


@click.command("deck:config:set")
@click.option("--deck", "deck_name", required=True, help="Deck name")
@click.option("--new-per-day", type=int, default=None)
@click.option("--reviews-per-day", type=int, default=None)
@click.option("--desired-retention", type=float, default=None)
@click.option("--maximum-review-interval", type=int, default=None)
@click.option("--learn-steps", default=None, help="Comma-separated values, in minutes.")
@click.option("--relearn-steps", default=None, help="Comma-separated values, in minutes.")
@click.pass_context
def deck_config_set_cmd(
    ctx: click.Context,
    deck_name: str,
    new_per_day: int | None,
    reviews_per_day: int | None,
    desired_retention: float | None,
    maximum_review_interval: int | None,
    learn_steps: str | None,
    relearn_steps: str | None,
) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)
    normalized = deck_name.strip()
    if not normalized:
        formatter.emit_error(
            command="deck:config:set",
            code="INVALID_INPUT",
            message="Deck name cannot be empty.",
        )
        raise click.exceptions.Exit(2)

    try:
        parsed_learn_steps = _parse_step_values(learn_steps)
        parsed_relearn_steps = _parse_step_values(relearn_steps)
    except ValueError as exc:
        formatter.emit_error(
            command="deck:config:set",
            code="INVALID_INPUT",
            message=f"Failed to parse step values: {exc}",
        )
        raise click.exceptions.Exit(2) from exc

    updates: dict[str, JSONValue] = {}
    if new_per_day is not None:
        updates["new_per_day"] = new_per_day
    if reviews_per_day is not None:
        updates["reviews_per_day"] = reviews_per_day
    if desired_retention is not None:
        updates["desired_retention"] = desired_retention
    if maximum_review_interval is not None:
        updates["maximum_review_interval"] = maximum_review_interval
    if parsed_learn_steps is not None:
        updates["learn_steps"] = parsed_learn_steps
    if parsed_relearn_steps is not None:
        updates["relearn_steps"] = parsed_relearn_steps

    if not updates:
        formatter.emit_error(
            command="deck:config:set",
            code="INVALID_INPUT",
            message="Provide at least one update option.",
        )
        raise click.exceptions.Exit(2)

    try:
        with backend_session_from_context(obj) as backend:
            data = backend.set_deck_config(normalized, updates)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_error(ctx=ctx, command="deck:config:set", obj=obj, error=exc, exit_code=7)
    except (LookupError, ValueError) as exc:
        formatter.emit_error(
            command="deck:config:set",
            code="BACKEND_OPERATION_FAILED",
            message=str(exc),
            details={"deck": normalized, "updates": updates},
        )
        raise click.exceptions.Exit(1) from exc

    formatter.emit_success(command="deck:config:set", data=data)


register_command("decks", decks_cmd)
register_command("deck", deck_cmd)
register_command("deck:create", deck_create_cmd)
register_command("deck:rename", deck_rename_cmd)
register_command("deck:delete", deck_delete_cmd)
register_command("deck:config", deck_config_cmd)
register_command("deck:config:set", deck_config_set_cmd)