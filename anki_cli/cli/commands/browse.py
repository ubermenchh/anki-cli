from __future__ import annotations

import click

from anki_cli.cli.command import CommandContext, anki_command

TUI_HINT = {"hint": "Run: uv sync --extra tui"}


@anki_command("browse")
@click.option("--query", default="", help="Anki search query")
def browse_cmd(cmd: CommandContext, query: str) -> None:
    """Browse cards interactively (TUI). For JSON, use ``cards``."""
    try:
        from anki_cli.tui.browse_app import BrowseApp
    except Exception as exc:
        raise cmd.fail(
            "TUI_NOT_AVAILABLE",
            f"Textual is not installed/available: {exc}",
            exit_code=2,
            details=TUI_HINT,
        ) from exc

    BrowseApp(backend=cmd.backend, query=query).run()
    return None
