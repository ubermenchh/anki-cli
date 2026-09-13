from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import click

from anki_cli.backends.ankiconnect import AnkiConnectAPIError, AnkiConnectProtocolError
from anki_cli.backends.factory import (
    BackendFactoryError,
    backend_session_from_context,
)
from anki_cli.cli.dispatcher import register_command
from anki_cli.cli.formatter import formatter_from_ctx
from anki_cli.core.scheduler import pick_next_due_card_id
from anki_cli.core.template import render_template
from anki_cli.core.undo import UndoItem, UndoStore, now_epoch_ms


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
        raise ValueError("rating must be one of: 1,2,3,4,again,hard,good,easy")
    return mapping[normalized]


def _extract_note_id(card: Mapping[str, Any]) -> int | None:
    for key in ("note", "nid", "noteId", "note_id"):
        value = card.get(key)
        if isinstance(value, int):
            return value
    return None


def _extract_ord(card: Mapping[str, Any]) -> int:
    value = card.get("ord")
    return int(value) if isinstance(value, int) else 0


def _pick_template(templates: Mapping[str, Any], ord_: int) -> Mapping[str, Any] | None:
    items = list(templates.items())

    for _name, tmpl in items:
        if isinstance(tmpl, Mapping) and isinstance(tmpl.get("ord"), int) and tmpl["ord"] == ord_:
            return cast(Mapping[str, Any], tmpl)

    if 0 <= ord_ < len(items):
        _name, tmpl = items[ord_]
        return tmpl if isinstance(tmpl, Mapping) else {}

    if items:
        _name, tmpl = items[0]
        return tmpl if isinstance(tmpl, Mapping) else {}

    return None


def _render_card(
    *,
    backend: Any,
    card_id: int,
    reveal_answer: bool,
) -> dict[str, Any]:
    card_obj = backend.get_card(card_id)
    card_map = cast(Mapping[str, Any], card_obj) if isinstance(card_obj, Mapping) else {}

    note_id = _extract_note_id(card_map)
    ord_ = _extract_ord(card_map)

    rendered: dict[str, Any] | None = None
    render_error: str | None = None

    if note_id is None:
        return {"card": card_obj, "rendered": None, "render_error": "Card has no note id."}

    fields_map = backend.get_note_fields(note_id=note_id, fields=None)

    notetype_name: str | None = None
    raw_nt = card_map.get("notetype_name")
    if isinstance(raw_nt, str) and raw_nt.strip():
        notetype_name = raw_nt.strip()
    else:
        note_obj = backend.get_note(note_id)
        if isinstance(note_obj, Mapping) and isinstance(note_obj.get("modelName"), str):
            notetype_name = str(note_obj["modelName"]).strip()

    if not notetype_name:
        return {"card": card_obj, "rendered": None, "render_error": "Unable to determine notetype."}

    nt_detail = backend.get_notetype(notetype_name)
    kind = str(nt_detail.get("kind", "normal")).lower()

    templates_raw = nt_detail.get("templates")
    templates_map: Mapping[str, Any]
    if isinstance(templates_raw, Mapping):
        templates_map = cast(Mapping[str, Any], templates_raw)
    else:
        templates_map = {}
    tpl = _pick_template(templates_map, ord_)

    if tpl is None:
        render_error = f"No templates found for notetype '{notetype_name}'."
        return {"card": card_obj, "rendered": None, "render_error": render_error}

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
        css = str(cast(Mapping[str, Any], styling_raw).get("css") or "")

    rendered = {
        "notetype": notetype_name,
        "ord": ord_,
        "question": question,
        "answer": answer,
        "css": css,
    }

    if not reveal_answer:
        rendered = dict(rendered)
        rendered.pop("answer", None)

    return {"card": card_obj, "rendered": rendered, "render_error": render_error}


@click.command("review")
@click.option("--deck", default=None, help="Optional deck filter")
@click.pass_context
def review_cmd(ctx: click.Context, deck: str | None) -> None:
    """Show due counts for review."""
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    try:
        with backend_session_from_context(obj) as backend:
            counts = backend.get_due_counts(deck=deck.strip() if deck else None)
    except (BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="review", obj=obj, error=exc)

    formatter.emit_success(
        command="review",
        data={"deck": deck, "due_counts": counts},
    )


@click.command("review:next")
@click.option("--deck", default=None, help="Optional deck filter")
@click.pass_context
def review_next_cmd(ctx: click.Context, deck: str | None) -> None:
    """Fetch the next due card (question only)."""
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    try:
        with backend_session_from_context(obj) as backend:
            card_id: int | None
            kind: str

            if getattr(backend, "name", "") == "direct" and hasattr(backend, "_store"):
                store = cast(Any, backend._store)
                if hasattr(store, "get_next_due_card"):
                    picked = store.get_next_due_card(deck.strip() if deck else None)
                    card_id = picked.get("card_id") if isinstance(picked, dict) else None
                    kind = str(picked.get("kind", "none")) if isinstance(picked, dict) else "none"
                else:
                    card_id, kind = pick_next_due_card_id(backend, deck=deck)
            else:
                card_id, kind = pick_next_due_card_id(backend, deck=deck)

            if not isinstance(card_id, int) or card_id <= 0:
                formatter.emit_success(
                    command="review:next",
                    data={"deck": deck, "card_id": None, "kind": kind},
                )
                return

            rendered = _render_card(backend=backend, card_id=card_id, reveal_answer=False)
    except (BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="review:next", obj=obj, error=exc)
    except (AnkiConnectAPIError, AnkiConnectProtocolError, LookupError, ValueError) as exc:
        formatter.emit_error(
            command="review:next",
            code="BACKEND_OPERATION_FAILED",
            message=str(exc),
            details={"deck": deck},
        )
        raise click.exceptions.Exit(1) from exc

    formatter.emit_success(
        command="review:next",
        data={
            "deck": deck,
            "kind": kind,
            "card_id": card_id,
            "question": (rendered.get("rendered") or {}).get("question"),
            "rendered": rendered.get("rendered"),
        },
    )


@click.command("review:show")
@click.option("--deck", default=None, help="Optional deck filter")
@click.pass_context
def review_show_cmd(ctx: click.Context, deck: str | None) -> None:
    """Show the next card with its answer."""
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    try:
        with backend_session_from_context(obj) as backend:
            card_id, kind = pick_next_due_card_id(backend, deck=deck)
            if card_id is None:
                formatter.emit_success(
                    command="review:show",
                    data={"deck": deck, "card_id": None, "kind": kind},
                )
                return
            rendered = _render_card(backend=backend, card_id=card_id, reveal_answer=True)
    except (BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="review:show", obj=obj, error=exc)
    except (AnkiConnectAPIError, AnkiConnectProtocolError, LookupError, ValueError) as exc:
        formatter.emit_error(
            command="review:show",
            code="BACKEND_OPERATION_FAILED",
            message=str(exc),
            details={"deck": deck},
        )
        raise click.exceptions.Exit(1) from exc

    formatter.emit_success(
        command="review:show",
        data={
            "deck": deck,
            "kind": kind,
            "card_id": card_id,
            "rendered": rendered.get("rendered"),
        },
    )


@click.command("review:preview")
@click.option("--id", "card_id", required=True, type=int, help="Card ID")
@click.pass_context
def review_preview_cmd(ctx: click.Context, card_id: int) -> None:
    """Preview scheduling outcome per rating."""
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    try:
        with backend_session_from_context(obj) as backend:
            if getattr(backend, "name", "") != "direct" or not hasattr(backend, "_store"):
                raise NotImplementedError("review:preview is supported only for direct backend.")
            store = cast(Any, backend._store)
            items = store.preview_ratings(int(card_id))
    except (BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="review:preview", obj=obj, error=exc)
    except (AnkiConnectAPIError, AnkiConnectProtocolError, LookupError, ValueError) as exc:
        formatter.emit_error(
            command="review:preview",
            code="BACKEND_OPERATION_FAILED",
            message=str(exc),
            details={"id": card_id},
        )
        raise click.exceptions.Exit(1) from exc

    formatter.emit_success(command="review:preview", data={"card_id": card_id, "items": items})


@click.command("review:undo")
@click.pass_context
def review_undo_cmd(ctx: click.Context) -> None:
    """Undo the last review answer."""
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    try:
        with backend_session_from_context(obj) as backend:
            if getattr(backend, "name", "") != "direct" or not hasattr(backend, "_store"):
                raise NotImplementedError("review:undo is supported only for direct backend.")

            col = getattr(backend, "collection_path", None)
            collection = str(col) if col is not None else ""
            store = UndoStore()
            item = store.pop(collection=collection)
            if item is None:
                formatter.emit_error(
                    command="review:undo",
                    code="UNDO_EMPTY",
                    message="No undo entries available.",
                )
                raise click.exceptions.Exit(2)

            direct_store = cast(Any, backend._store)
            result = direct_store.restore_card_state(item.snapshot)
    except (BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="review:undo", obj=obj, error=exc)
    except (LookupError, ValueError) as exc:
        formatter.emit_error(
            command="review:undo",
            code="BACKEND_OPERATION_FAILED",
            message=str(exc),
        )
        raise click.exceptions.Exit(1) from exc

    formatter.emit_success(command="review:undo", data=result)


@click.command("review:answer")
@click.option("--id", "card_id", required=True, type=int, help="Card ID")
@click.option("--rating", required=True, help="Rating: again|hard|good|easy or 1..4")
@click.pass_context
def review_answer_cmd(ctx: click.Context, card_id: int, rating: str) -> None:
    """Answer a card (again/hard/good/easy)."""
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
            # Save undo snapshot (direct backend only). The snapshot must
            # capture pre-answer state, but it is pushed only after
            # answer_card succeeds so a failed answer cannot leave a stale
            # undo entry.
            snapshot: dict[str, Any] | None = None
            collection = ""
            if getattr(backend, "name", "") == "direct" and hasattr(backend, "_store"):
                col = getattr(backend, "collection_path", None)
                collection = str(col) if col is not None else ""
                direct_store = cast(Any, backend._store)
                snapshot = cast(dict[str, Any], direct_store.snapshot_card_state(int(card_id)))

            result = backend.answer_card(card_id=int(card_id), ease=ease)

            if snapshot is not None:
                # Carry the id of the revlog row this answer wrote so undo can
                # delete exactly that row instead of a time window.
                revlog_id = result.get("revlog_id") if isinstance(result, Mapping) else None
                undo_item = UndoItem(
                    collection=collection,
                    card_id=int(card_id),
                    snapshot={**snapshot, "revlog_id": revlog_id},
                    created_at_epoch_ms=now_epoch_ms(),
                )
                # Best-effort: the answer is already committed, so a failed
                # undo write must not fail the command (a retry would apply a
                # second review).
                try:
                    UndoStore().push(undo_item)
                except OSError as exc:
                    click.echo(
                        f"warning: answer saved but undo entry not written: {exc}",
                        err=True,
                    )
    except (BackendFactoryError, NotImplementedError) as exc:
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

@click.command("review:start")
@click.option("--deck", default=None, help="Optional deck filter")
@click.pass_context
def review_start_cmd(ctx: click.Context, deck: str | None) -> None:
    """Start an interactive review session (TUI)."""
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    try:
        from anki_cli.tui.review_app import ReviewApp
    except Exception as exc:
        formatter.emit_error(
            command="review:start",
            code="TUI_NOT_AVAILABLE",
            message=f"Textual is not installed/available: {exc}",
            details={"hint": "Run: uv sync --extra tui"},
        )
        raise click.exceptions.Exit(2) from exc

    try:
        with backend_session_from_context(obj) as backend:
            if getattr(backend, "name", "") != "direct":
                formatter.emit_error(
                    command="review:start",
                    code="UNSUPPORTED_BACKEND",
                    message="review:start currently supports only direct backend.",
                    details={"backend": getattr(backend, "name", "unknown")},
                )
                raise click.exceptions.Exit(2)

            app = ReviewApp(backend=backend, deck=deck.strip() if deck else None)
            app.run()
    except (BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="review:start", obj=obj, error=exc)


register_command("review", review_cmd)
register_command("review:next", review_next_cmd)
register_command("review:show", review_show_cmd)
register_command("review:answer", review_answer_cmd)
register_command("review:preview", review_preview_cmd)
register_command("review:undo", review_undo_cmd)
register_command("review:start", review_start_cmd)
