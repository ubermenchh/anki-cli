from __future__ import annotations

from pathlib import Path

import click
from click.core import ParameterSource

from anki_cli import __version__
from anki_cli.backends.detect import DetectionError, detect_backend
from anki_cli.cli.dispatcher import get_command, list_commands
from anki_cli.cli.formatter import formatter_from_ctx
from anki_cli.cli.params import preprocess_argv
from anki_cli.config_runtime import ConfigError, resolve_runtime_config


def _print_version(ctx: click.Context, param: click.Option, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    click.echo(f"anki-cli {__version__}")
    raise click.exceptions.Exit()


def _is_set_on_cli(ctx: click.Context, param_name: str) -> bool:
    return ctx.get_parameter_source(param_name) is ParameterSource.COMMANDLINE


class NamespaceGroup(click.Group):
    """Click group with dynamic command discovery and key=value preprocessing."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        transformed = preprocess_argv(args)
        return super().parse_args(ctx, transformed)

    def list_commands(self, ctx: click.Context) -> list[str]:
        return list_commands()

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        return get_command(cmd_name)


@click.group(
    cls=NamespaceGroup,
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["table", "json", "md", "csv", "plain"], case_sensitive=False),
    default="table",
    show_default=True,
    help="Output format.",
)
@click.option("--col", "collection_path", type=click.Path(path_type=Path), default=None)
@click.option(
    "--backend",
    type=click.Choice(["auto", "ankiconnect", "direct", "standalone"], case_sensitive=False),
    default="auto",
    show_default=True,
)
@click.option("--quiet", is_flag=True, default=False)
@click.option("--verbose", is_flag=True, default=False)
@click.option("--no-color", is_flag=True, default=False)
@click.option("--yes", is_flag=True, default=False)
@click.option("--copy", is_flag=True, default=False)
@click.option(
    "--version",
    "show_version",
    is_flag=True,
    expose_value=False,
    is_eager=True,
    callback=_print_version,
    help="Show version and exit.",
)
@click.pass_context
def main(
    ctx: click.Context,
    output_format: str,
    collection_path: Path | None,
    backend: str,
    quiet: bool,
    verbose: bool,
    no_color: bool,
    yes: bool,
    copy: bool,
) -> None:
    ctx.ensure_object(dict)
    ctx.obj.update(
        {
            "format": output_format.lower(),
            "collection_path": collection_path,
            "backend": "none",
            "quiet": quiet,
            "verbose": verbose,
            "no_color": no_color,
            "yes": yes,
            "copy": copy,
        }
    )

    try:
        runtime = resolve_runtime_config(
            cli_backend=backend,
            cli_backend_set=_is_set_on_cli(ctx, "backend"),
            cli_output_format=output_format,
            cli_output_set=_is_set_on_cli(ctx, "output_format"),
            cli_no_color=no_color,
            cli_no_color_set=_is_set_on_cli(ctx, "no_color"),
            cli_collection_path=collection_path,
            cli_collection_set=_is_set_on_cli(ctx, "collection_path"),
        )
    except ConfigError as exc:
        formatter = formatter_from_ctx(ctx)
        formatter.emit_error(
            command="bootstrap",
            code="INVALID_CONFIG",
            message=str(exc),
        )
        raise click.exceptions.Exit(2) from exc

    ctx.obj.update(
        {
            "format": runtime.output_format,
            "no_color": runtime.no_color,
            "requested_backend": runtime.backend,
            "config_path": runtime.config_path,
            "app_config": runtime.app,
            "collection_override": runtime.collection_override,
        }
    )

    try:
        detection = detect_backend(
            forced_backend=runtime.backend,
            col_override=runtime.collection_override,
            ankiconnect_url=runtime.app.backend.ankiconnect_url,
        )
    except DetectionError as exc:
        formatter = formatter_from_ctx(ctx)
        formatter.emit_error(
            command="bootstrap",
            code="BACKEND_UNAVAILABLE",
            message=str(exc),
            details={"forced_backend": runtime.backend},
        )
        raise click.exceptions.Exit(exc.exit_code) from exc

    ctx.obj.update(
        {
            "collection_path": detection.collection_path,
            "backend": detection.backend,
            "backend_reason": detection.reason,
        }
    )

    if ctx.invoked_subcommand is None:
        try:
            from anki_cli.tui.repl import run_repl
            run_repl(ctx.obj)
        except ImportError:
            click.echo(ctx.get_help())