from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

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
from anki_cli.core.template import render_template


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


def _collect_card_ids(*, backend: Any, card_id: int | None, query: str | None) -> list[int]:
    if card_id is not None:
        return [card_id]
    return backend.find_cards(query=query or "")


@click.command("cards:ids")
@click.option("--query", default="", help="Anki search query")
@click.pass_context
def cards_ids_cmd(ctx: click.Context, query: str) -> None:
    """List card IDs matching a query."""
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    try:
        with backend_session_from_context(obj) as backend:
            ids = backend.find_cards(query=query)
    except SearchParseError as exc:
        _emit_invalid_query(ctx=ctx, command="cards:ids", query=query, error=exc)
    except AnkiConnectAPIError as exc:
        _emit_invalid_query(ctx=ctx, command="cards:ids", query=query, error=exc)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="cards:ids", obj=obj, error=exc)

    formatter.emit_success(
        command="cards:ids",
        data={"query": query, "count": len(ids), "ids": ids},
    )


def _extract_note_id(card: Mapping[str, Any]) -> int | None:
    for key in ("note", "nid", "noteId", "note_id"):
        value = card.get(key)
        if isinstance(value, int):
            return value
    return None


def _extract_ord(card: Mapping[str, Any]) -> int:
    value = card.get("ord")
    return int(value) if isinstance(value, int) else 0


def _pick_template(
    templates: Mapping[str, Any],
    ord_: int,
) -> tuple[str, Mapping[str, Any]] | None:
    items = list(templates.items())

    # Prefer explicit ord if present (direct backend provides it)
    for name, tmpl in items:
        if isinstance(tmpl, Mapping) and isinstance(tmpl.get("ord"), int) and tmpl["ord"] == ord_:
            return str(name), cast(Mapping[str, Any], tmpl)

    # Fallback: index into mapping insertion order (best we can do for AnkiConnect)
    if 0 <= ord_ < len(items):
        name, tmpl = items[ord_]
        return str(name), tmpl if isinstance(tmpl, Mapping) else {}
    if items:
        name, tmpl = items[0]
        return str(name), tmpl if isinstance(tmpl, Mapping) else {}

    return None


@click.command("card")
@click.option("--id", "card_id", required=True, type=int, help="Card ID")
@click.option("--revlog-limit", default=10, type=int, show_default=True)
@click.pass_context
def card_cmd(ctx: click.Context, card_id: int, revlog_limit: int) -> None:
    """Show detailed info for a single card."""
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    try:
        with backend_session_from_context(obj) as backend:
            card_obj = backend.get_card(card_id)
            note_id = _extract_note_id(cast(Mapping[str, Any], card_obj))
            ord_ = _extract_ord(cast(Mapping[str, Any], card_obj))

            rendered: dict[str, Any] | None = None
            render_error: str | None = None

            if note_id is not None:
                # Field name -> value mapping (works for direct + ankiconnect)
                fields_map = backend.get_note_fields(note_id=note_id, fields=None)

                # Determine notetype name
                notetype_name: str | None = None
                raw_nt = cast(Mapping[str, Any], card_obj).get("notetype_name")
                if isinstance(raw_nt, str) and raw_nt.strip():
                    notetype_name = raw_nt.strip()
                else:
                    note_obj = backend.get_note(note_id)
                    if isinstance(note_obj, Mapping) and isinstance(note_obj.get("modelName"), str):
                        notetype_name = str(note_obj["modelName"]).strip()
                    elif isinstance(note_obj, Mapping):
                        # Fallback: scan notetype list to find name (slower, but backend-agnostic)
                        mid_raw = note_obj.get("mid")
                        if isinstance(mid_raw, int):
                            mid = mid_raw
                            for nt in backend.get_notetypes():
                                if isinstance(nt, Mapping) and nt.get("id") == mid:
                                    name_raw = nt.get("name")
                                    if isinstance(name_raw, str):
                                        notetype_name = name_raw.strip()
                                    break

                if notetype_name:
                    nt_detail = backend.get_notetype(notetype_name)
                    kind = str(nt_detail.get("kind", "normal")).lower()
                    templates_raw = nt_detail.get("templates")

                    templates_map: Mapping[str, Any]
                    if isinstance(templates_raw, Mapping):
                        templates_map = cast(Mapping[str, Any], templates_raw)
                    else:
                        templates_map = {}

                    picked = _pick_template(templates_map, ord_)
                    if picked is None:
                        render_error = f"No templates found for notetype '{notetype_name}'."
                    else:
                        _tpl_name, tpl = picked
                        front_tmpl = str(tpl.get("Front") or "")
                        back_tmpl = str(tpl.get("Back") or "")

                        if kind == "cloze":
                            cloze_index = ord_ + 1
                            question = render_template(
                                front_tmpl,
                                fields_map,
                                cloze_index=cloze_index,
                                reveal_cloze=False,
                            )
                            answer = render_template(
                                back_tmpl,
                                fields_map,
                                front_side=question,
                                cloze_index=cloze_index,
                                reveal_cloze=True,
                            )
                        else:
                            question = render_template(front_tmpl, fields_map)
                            answer = render_template(back_tmpl, fields_map, front_side=question)

                        css = ""
                        styling_raw = nt_detail.get("styling")
                        if isinstance(styling_raw, Mapping):
                            styling_map = cast(Mapping[str, Any], styling_raw)
                            css = str(styling_map.get("css") or "")

                        rendered = {
                            "notetype": notetype_name,
                            "ord": ord_,
                            "question": question,
                            "answer": answer,
                            "css": css,
                        }

            # Revlog is optional: AnkiConnect backend currently raises NotImplementedError
            revlog: list[dict[str, Any]] | None = None
            try:
                bounded = max(1, min(int(revlog_limit), 1000))
                revlog = backend.get_revlog(card_id=card_id, limit=bounded)
            except NotImplementedError:
                revlog = None

    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="card", obj=obj, error=exc)
    except (AnkiConnectAPIError, LookupError) as exc:
        formatter.emit_error(
            command="card",
            code="ENTITY_NOT_FOUND",
            message=str(exc),
            details={"id": card_id},
        )
        raise click.exceptions.Exit(4) from exc

    out = dict(card_obj) if isinstance(card_obj, dict) else {"card": card_obj}
    out["rendered"] = rendered
    if render_error:
        out["render_error"] = render_error
    if revlog is not None:
        out["revlog"] = revlog

    formatter.emit_success(command="card", data=out)


@click.command("card:suspend")
@click.option("--id", "card_id", type=int, default=None, help="Card ID")
@click.option("--query", default=None, help="Search query for target cards")
@click.pass_context
def card_suspend_cmd(ctx: click.Context, card_id: int | None, query: str | None) -> None:
    """Suspend cards by ID or query."""
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
    except SearchParseError as exc:
        _emit_invalid_query(ctx=ctx, command="card:suspend", query=query, error=exc)
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
    """Unsuspend cards by ID or query."""
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
    except SearchParseError as exc:
        _emit_invalid_query(ctx=ctx, command="card:unsuspend", query=query, error=exc)
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


@click.command("card:revlog")
@click.option("--id", "card_id", required=True, type=int, help="Card ID")
@click.option("--limit", default=50, type=int, show_default=True, help="Max revlog rows (1..1000)")
@click.pass_context
def card_revlog_cmd(ctx: click.Context, card_id: int, limit: int) -> None:
    """Show review history for a card."""
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    bounded_limit = max(1, min(limit, 1000))

    try:
        with backend_session_from_context(obj) as backend:
            entries = backend.get_revlog(card_id=card_id, limit=bounded_limit)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="card:revlog", obj=obj, error=exc)
    except (AnkiConnectAPIError, LookupError) as exc:
        formatter.emit_error(
            command="card:revlog",
            code="ENTITY_NOT_FOUND",
            message=str(exc),
            details={"id": card_id},
        )
        raise click.exceptions.Exit(4) from exc

    formatter.emit_success(
        command="card:revlog",
        data={
            "id": card_id,
            "limit": bounded_limit,
            "count": len(entries),
            "items": entries,
        },
    )


@click.command("card:move")
@click.option("--id", "card_id", type=int, default=None, help="Card ID")
@click.option("--query", default=None, help="Search query for target cards")
@click.option("--deck", "deck_name", required=True, help="Destination deck name")
@click.pass_context
def card_move_cmd(
    ctx: click.Context, card_id: int | None, query: str | None, deck_name: str
) -> None:
    """Move cards to a different deck."""
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)
    if card_id is None and not query:
        formatter.emit_error(
            command="card:move", code="INVALID_INPUT", message="Provide --id or --query."
        )
        raise click.exceptions.Exit(2)

    try:
        with backend_session_from_context(obj) as backend:
            ids = _collect_card_ids(backend=backend, card_id=card_id, query=query)
            result = backend.move_cards(ids, deck_name.strip())
    except SearchParseError as exc:
        _emit_invalid_query(ctx=ctx, command="card:move", query=query, error=exc)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="card:move", obj=obj, error=exc)
    except (AnkiConnectAPIError, LookupError, ValueError) as exc:
        formatter.emit_error(command="card:move", code="BACKEND_OPERATION_FAILED", message=str(exc))
        raise click.exceptions.Exit(1) from exc

    formatter.emit_success(command="card:move", data=result)


@click.command("card:flag")
@click.option("--id", "card_id", type=int, default=None, help="Card ID")
@click.option("--query", default=None, help="Search query for target cards")
@click.option("--flag", type=int, required=True, help="Flag value 0..7")
@click.pass_context
def card_flag_cmd(ctx: click.Context, card_id: int | None, query: str | None, flag: int) -> None:
    """Set a flag (0-7) on cards."""
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)
    if card_id is None and not query:
        formatter.emit_error(
            command="card:flag", code="INVALID_INPUT", message="Provide --id or --query."
        )
        raise click.exceptions.Exit(2)

    try:
        with backend_session_from_context(obj) as backend:
            ids = _collect_card_ids(backend=backend, card_id=card_id, query=query)
            result = backend.set_card_flag(ids, flag)
    except SearchParseError as exc:
        _emit_invalid_query(ctx=ctx, command="card:flag", query=query, error=exc)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="card:flag", obj=obj, error=exc)
    except (AnkiConnectAPIError, LookupError, ValueError) as exc:
        formatter.emit_error(command="card:flag", code="BACKEND_OPERATION_FAILED", message=str(exc))
        raise click.exceptions.Exit(1) from exc

    formatter.emit_success(command="card:flag", data=result)


@click.command("card:bury")
@click.option("--id", "card_id", type=int, default=None, help="Card ID")
@click.option("--query", default=None, help="Search query for target cards")
@click.pass_context
def card_bury_cmd(ctx: click.Context, card_id: int | None, query: str | None) -> None:
    """Bury cards until next session."""
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)
    if card_id is None and not query:
        formatter.emit_error(
            command="card:bury", code="INVALID_INPUT", message="Provide --id or --query."
        )
        raise click.exceptions.Exit(2)

    try:
        with backend_session_from_context(obj) as backend:
            ids = _collect_card_ids(backend=backend, card_id=card_id, query=query)
            result = backend.bury_cards(ids)
    except SearchParseError as exc:
        _emit_invalid_query(ctx=ctx, command="card:bury", query=query, error=exc)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="card:bury", obj=obj, error=exc)
    except (AnkiConnectAPIError, LookupError, ValueError) as exc:
        formatter.emit_error(command="card:bury", code="BACKEND_OPERATION_FAILED", message=str(exc))
        raise click.exceptions.Exit(1) from exc

    formatter.emit_success(command="card:bury", data=result)


@click.command("card:unbury")
@click.option("--deck", "deck_name", default=None, help="Optional deck scope")
@click.pass_context
def card_unbury_cmd(ctx: click.Context, deck_name: str | None) -> None:
    """Unbury all cards in a deck."""
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)
    deck = deck_name.strip() if deck_name else None

    try:
        with backend_session_from_context(obj) as backend:
            result = backend.unbury_cards(deck=deck)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="card:unbury", obj=obj, error=exc)
    except (AnkiConnectAPIError, LookupError, ValueError) as exc:
        formatter.emit_error(
            command="card:unbury", code="BACKEND_OPERATION_FAILED", message=str(exc)
        )
        raise click.exceptions.Exit(1) from exc

    formatter.emit_success(command="card:unbury", data=result)


@click.command("card:reschedule")
@click.option("--id", "card_id", type=int, default=None, help="Card ID")
@click.option("--query", default=None, help="Search query for target cards")
@click.option("--days", type=int, required=True, help="Days from today")
@click.pass_context
def card_reschedule_cmd(
    ctx: click.Context, card_id: int | None, query: str | None, days: int
) -> None:
    """Reschedule cards to N days from today."""
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)
    if card_id is None and not query:
        formatter.emit_error(
            command="card:reschedule", code="INVALID_INPUT", message="Provide --id or --query."
        )
        raise click.exceptions.Exit(2)

    try:
        with backend_session_from_context(obj) as backend:
            ids = _collect_card_ids(backend=backend, card_id=card_id, query=query)
            result = backend.reschedule_cards(ids, days)
    except SearchParseError as exc:
        _emit_invalid_query(ctx=ctx, command="card:reschedule", query=query, error=exc)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="card:reschedule", obj=obj, error=exc)
    except (AnkiConnectAPIError, LookupError, ValueError) as exc:
        formatter.emit_error(
            command="card:reschedule", code="BACKEND_OPERATION_FAILED", message=str(exc)
        )
        raise click.exceptions.Exit(1) from exc

    formatter.emit_success(command="card:reschedule", data=result)


@click.command("card:reset")
@click.option("--id", "card_id", type=int, default=None, help="Card ID")
@click.option("--query", default=None, help="Search query for target cards")
@click.pass_context
def card_reset_cmd(ctx: click.Context, card_id: int | None, query: str | None) -> None:
    """Reset cards to new (forget progress)."""
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)
    if card_id is None and not query:
        formatter.emit_error(
            command="card:reset", code="INVALID_INPUT", message="Provide --id or --query."
        )
        raise click.exceptions.Exit(2)

    try:
        with backend_session_from_context(obj) as backend:
            ids = _collect_card_ids(backend=backend, card_id=card_id, query=query)
            result = backend.reset_cards(ids)
    except SearchParseError as exc:
        _emit_invalid_query(ctx=ctx, command="card:reset", query=query, error=exc)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="card:reset", obj=obj, error=exc)
    except (AnkiConnectAPIError, LookupError, ValueError) as exc:
        formatter.emit_error(
            command="card:reset", code="BACKEND_OPERATION_FAILED", message=str(exc)
        )
        raise click.exceptions.Exit(1) from exc

    formatter.emit_success(command="card:reset", data=result)


register_command("cards:ids", cards_ids_cmd)
register_command("card", card_cmd)
register_command("card:suspend", card_suspend_cmd)
register_command("card:unsuspend", card_unsuspend_cmd)
register_command("card:revlog", card_revlog_cmd)
register_command("card:move", card_move_cmd)
register_command("card:flag", card_flag_cmd)
register_command("card:bury", card_bury_cmd)
register_command("card:unbury", card_unbury_cmd)
register_command("card:reschedule", card_reschedule_cmd)
register_command("card:reset", card_reset_cmd)
