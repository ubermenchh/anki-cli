from __future__ import annotations

from anki_cli.cli.command import CommandContext, anki_command


@anki_command("shell")
def shell_cmd(cmd: CommandContext) -> None:
    """Launch interactive shell (REPL)."""
    try:
        from anki_cli.tui.repl import run_repl
    except Exception as exc:  # same TUI_NOT_AVAILABLE path as browse/review:start
        raise cmd.fail(
            "TUI_NOT_AVAILABLE",
            f"prompt_toolkit is not installed: {exc}",
            exit_code=2,
            details={"hint": "Run: uv sync --extra tui"},
        ) from exc

    run_repl(cmd.obj)
    return None
