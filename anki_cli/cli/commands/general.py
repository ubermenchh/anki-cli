from __future__ import annotations

import platform
from pathlib import Path
from typing import Any

import click

from anki_cli import __version__
from anki_cli.backends.detect import DetectionError, detect_backend
from anki_cli.cli.dispatcher import register_command
from anki_cli.cli.formatter import formatter_from_ctx
from anki_cli.models.config import AppConfig


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


register_command("version", version_cmd)
register_command("status", status_cmd)
