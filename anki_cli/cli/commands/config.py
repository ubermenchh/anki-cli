from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from anki_cli.cli.dispatcher import register_command
from anki_cli.cli.formatter import formatter_from_ctx
from anki_cli.config_runtime import ConfigError, set_config_value
from anki_cli.models.config import AppConfig


@click.command("config")
@click.pass_context
def config_cmd(ctx: click.Context) -> None:
    """Show current configuration."""
    obj: dict[str, Any] = ctx.obj or {}
    app_config = obj.get("app_config")

    if isinstance(app_config, AppConfig):
        config_data: dict[str, Any] = app_config.model_dump(mode="json")
    else:
        config_data = {}

    collection_override = obj.get("collection_override")
    config_path = obj.get("config_path")

    formatter = formatter_from_ctx(ctx)
    formatter.emit_success(
        command="config",
        data={
            "config_path": str(config_path) if config_path is not None else None,
            "effective": {
                "backend": str(obj.get("requested_backend", "auto")),
                "output_format": str(obj.get("format", "table")),
                "color": not bool(obj.get("no_color", False)),
                "collection_override": (
                    str(collection_override) if collection_override is not None else None
                ),
            },
            "config": config_data,
        },
    )


@click.command("config:path")
@click.pass_context
def config_path_cmd(ctx: click.Context) -> None:
    """Show file paths used by anki-cli."""
    obj: dict[str, Any] = ctx.obj or {}

    col_path = obj.get("collection_path")
    config_path = obj.get("config_path") or Path("~/.config/anki-cli/config.toml").expanduser()
    anki_profiles = Path("~/.local/share/Anki2").expanduser()

    formatter = formatter_from_ctx(ctx)
    formatter.emit_success(
        command="config:path",
        data={
            "collection": str(col_path) if col_path is not None else "(auto)",
            "config": str(config_path),
            "anki_profiles": str(anki_profiles),
        },
    )


@click.command("config:set")
@click.option("--key", required=True, help="Dotted key path, e.g. display.color")
@click.option("--value", required=True, help="New value")
@click.pass_context
def config_set_cmd(ctx: click.Context, key: str, value: str) -> None:
    """Set a config value by dotted key path."""
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    raw_path = obj.get("config_path")
    config_path = Path(raw_path) if isinstance(raw_path, (str, Path)) else None

    try:
        loaded, old_value, new_value = set_config_value(
            key=key,
            raw_value=value,
            config_path=config_path,
        )
    except ConfigError as exc:
        formatter.emit_error(
            command="config:set",
            code="INVALID_CONFIG",
            message=str(exc),
            details={"key": key, "value": value},
        )
        raise click.exceptions.Exit(2) from exc

    formatter.emit_success(
        command="config:set",
        data={
            "config_path": str(loaded.config_path),
            "key": key,
            "old_value": old_value,
            "new_value": new_value,
        },
    )


register_command("config", config_cmd)
register_command("config:path", config_path_cmd)
register_command("config:set", config_set_cmd)
