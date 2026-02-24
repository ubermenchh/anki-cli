from __future__ import annotations

from typing import Any

import click

from anki_cli.backends.factory import (
    BackendFactoryError,
    BackendNotImplementedError,
    backend_session_from_context,
)
from anki_cli.cli.dispatcher import register_command
from anki_cli.cli.formatter import formatter_from_ctx


@click.command("cards")
@click.option("--query", default="", help="Anki search query")
@click.pass_context
def cards_cmd(ctx: click.Context, query: str) -> None:
    """Browse cards interactively (TUI)."""
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    try:
        from anki_cli.tui.browse_app import BrowseApp
    except Exception as exc:
        formatter.emit_error(
            command="cards",
            code="TUI_NOT_AVAILABLE",
            message=f"Textual is not installed/available: {exc}",
            details={"hint": "Run: uv sync --extra tui"},
        )
        raise click.exceptions.Exit(2) from exc

    try:
        with backend_session_from_context(obj) as backend:
            app = BrowseApp(backend=backend, query=query)
            app.run()
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        formatter.emit_error(
            command="cards",
            code="BACKEND_UNAVAILABLE",
            message=str(exc),
            details={"backend": str(obj.get("backend", "unknown"))},
        )
        raise click.exceptions.Exit(7) from exc


register_command("cards", cards_cmd)
