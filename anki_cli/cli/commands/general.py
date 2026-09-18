from __future__ import annotations

import platform
from pathlib import Path
from typing import Any

import click

from anki_cli import __version__
from anki_cli.backends.detect import DetectionError, detect_backend
from anki_cli.cli.command import CommandContext, anki_command
from anki_cli.cli.dispatcher import register_command
from anki_cli.cli.formatter import formatter_from_ctx
from anki_cli.models.config import AppConfig
from anki_cli.models.output import JSONValue


@anki_command("version")
def version_cmd(cmd: CommandContext) -> JSONValue:
    """Show version and environment info."""
    col = cmd.obj.get("collection_path")
    return {
        "version": __version__,
        "python": platform.python_version(),
        "backend": str(cmd.obj.get("backend", "none")),
        "collection": str(col) if col is not None else None,
    }


@click.command("status")
@click.pass_context
def status_cmd(ctx: click.Context) -> None:
    """Probe backend/collection health and report it — exit 0 either way."""
    obj: dict[str, Any] = ctx.obj or {}
    app_config = obj.get("app_config")
    cfg = app_config if isinstance(app_config, AppConfig) else AppConfig()

    col_override = obj.get("collection_override")
    try:
        detection = detect_backend(
            forced_backend=str(obj.get("requested_backend", "auto")),
            col_override=col_override if isinstance(col_override, Path) else None,
            ankiconnect_url=cfg.backend.ankiconnect_url,
            anki_profile=cfg.collection.anki_profile,
            allow_non_localhost=cfg.backend.allow_non_localhost,
        )
    except DetectionError as exc:
        # Detection failure is the report, not an error — exit stays 0.
        data: dict[str, Any] = {
            "ok": False,
            "backend": None,
            "collection": None,
            "error": str(exc),
        }
    else:
        data = {
            "ok": True,
            "backend": detection.backend,
            "collection": (
                str(detection.collection_path)
                if detection.collection_path is not None
                else None
            ),
            "reason": detection.reason,
            "profile": detection.profile,
        }

    formatter = formatter_from_ctx(ctx)
    formatter.emit_success(command="status", data=data)


register_command("status", status_cmd)


@anki_command("commands")
def commands_cmd(cmd: CommandContext) -> JSONValue:
    """Machine-readable command reference: every command, its options, and the
    error / exit codes — generated from the registry so it cannot drift (#28)."""
    from anki_cli.cli.dispatcher import get_command, list_commands
    from anki_cli.models.output import EXIT_CODE_MEANINGS, ErrorCode, ExitCode

    items: list[dict[str, Any]] = []
    for name in list_commands():
        command = get_command(name)
        if command is None:
            continue
        options: list[dict[str, Any]] = []
        for param in command.params:
            if not isinstance(param, click.Option):
                continue
            options.append(
                {
                    "spellings": [*param.opts, *param.secondary_opts],
                    "required": bool(param.required),
                    "flag": bool(param.is_flag),
                    "multiple": bool(param.multiple),
                    "type": param.type.name,
                    "help": param.help or "",
                }
            )
        items.append(
            {
                "name": name,
                "hidden": bool(command.hidden),
                "help": (command.help or "").strip(),
                "options": options,
            }
        )

    return {
        "count": len(items),
        "items": items,
        "global_options": ["--format", "--backend", "--col", "--yes", "--copy", "--no-color"],
        "exit_codes": {str(int(c)): EXIT_CODE_MEANINGS[c] for c in ExitCode},
        "error_codes": [str(c) for c in ErrorCode],
    }
