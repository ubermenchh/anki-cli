from __future__ import annotations

import platform
from typing import Any

import click

from anki_cli import __version__
from anki_cli.cli.dispatcher import register_command
from anki_cli.cli.formatter import formatter_from_ctx


@click.command("version")
@click.pass_context
def version_cmd(ctx: click.Context) -> None:
    """Show version and environment info."""
    obj: dict[str, Any] = ctx.obj or {}
    backend = str(obj.get("backend", "none"))
    col = obj.get("collection_path")

    formatter = formatter_from_ctx(ctx)
    formatter.emit_success(
        command="version",
        data={
            "version": __version__,
            "python": platform.python_version(),
            "backend": backend,
            "collection": str(col) if col is not None else None,
        },
    )


@click.command("status")
@click.pass_context
def status_cmd(ctx: click.Context) -> None:
    """Show current backend and collection status."""
    obj: dict[str, Any] = ctx.obj or {}
    col = obj.get("collection_path")

    formatter = formatter_from_ctx(ctx)
    formatter.emit_success(
        command="status",
        data={
            "backend": str(obj.get("backend", "unknown")),
            "collection": str(col) if col is not None else None,
        },
    )


register_command("version", version_cmd)
register_command("status", status_cmd)
