from __future__ import annotations

from typing import Any

import click

from anki_cli.cli.dispatcher import register_command
from anki_cli.cli.formatter import formatter_from_ctx


@click.command("shell")
@click.pass_context
def shell_cmd(ctx: click.Context) -> None:
    """Launch interactive shell (REPL)."""
    obj: dict[str, Any] = ctx.obj or {}

    try:
        from anki_cli.tui.repl import run_repl
    except ImportError as exc:
        formatter = formatter_from_ctx(ctx)
        formatter.emit_error(
            command="shell",
            code="TUI_NOT_AVAILABLE",
            message=f"prompt_toolkit is not installed: {exc}",
            details={"hint": "Run: uv sync --extra tui"},
        )
        raise click.exceptions.Exit(2) from exc

    run_repl(obj)


register_command("shell", shell_cmd)
