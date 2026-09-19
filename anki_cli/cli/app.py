from __future__ import annotations

import sys
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import Any, get_args

import click
from click.core import ParameterSource

from anki_cli import __version__
from anki_cli.backends.detect import DetectionError, detect_backend
from anki_cli.backends.factory import DETECTION_PENDING
from anki_cli.cli.dispatcher import get_command, list_commands
from anki_cli.cli.errors import classify, debug_tracebacks_enabled
from anki_cli.cli.formatter import OutputFormatter, formatter_from_ctx
from anki_cli.cli.params import (
    DanglingOptionError,
    command_name_from_argv,
    hoist_group_options,
    option_arity,
    peek_output_options,
    preprocess_argv,
)
from anki_cli.config_runtime import ConfigError, resolve_runtime_config
from anki_cli.models.config import BackendPreference, OutputFormat


def _print_version(ctx: click.Context, param: click.Option, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    click.echo(f"anki-cli {__version__}")
    raise click.exceptions.Exit()


def _is_set_on_cli(ctx: click.Context, param_name: str) -> bool:
    return ctx.get_parameter_source(param_name) is ParameterSource.COMMANDLINE


# Commands that never need a backend — they skip detection entirely so they
# still run (and exit 0) on hosts with no Anki install. `status` probes for
# itself inside the command so it can report failure as data. The REPL
# (invoked_subcommand=None) is NOT here: it must detect up front, degrading
# to a warning + backend="none" when detection fails instead of exiting.
_BACKENDLESS = {"version", "status", "config", "config:path", "config:set", "commands"}


class NamespaceGroup(click.Group):
    """Click group with dynamic command discovery, key=value preprocessing,
    group options accepted after the subcommand (#26), and a JSON error
    envelope for every failure that escapes a command (#27)."""

    # The context Click built for this invocation. Only ``click.UsageError``
    # carries a ``.ctx``; for every other exception this is how the envelope
    # reaches the resolved output format / backend the group callback stored
    # in ``ctx.obj`` (subcommand contexts share the same ``obj`` dict).
    _root_ctx: click.Context | None = None

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: click.Context | None = None,
        **extra: Any,
    ) -> click.Context:
        ctx = super().make_context(info_name, args, parent, **extra)
        if parent is None:
            self._root_ctx = ctx
        return ctx

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        group_options = option_arity(self)

        def _resolve(name: str):
            cmd = get_command(name)
            return option_arity(cmd) if cmd is not None else None

        transformed = preprocess_argv(
            args, group_options=group_options, resolve_command_options=_resolve
        )
        try:
            transformed = hoist_group_options(
                transformed,
                group_options=group_options,
                is_command=_is_command,
                resolve_command_options=_resolve,
            )
        except DanglingOptionError as exc:
            raise click.UsageError(str(exc), ctx=ctx) from exc
        return super().parse_args(ctx, transformed)

    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        **extra: Any,
    ) -> Any:
        """Standalone entry point that never lets a failure bypass the envelope.

        Click's own standalone mode prints usage errors as plain text and lets
        everything else traceback. We run it non-standalone, so ``UsageError``,
        ``Abort`` (Click's rendering of Ctrl-C — note Click echoes one bare
        newline to stderr before raising it, so the INTERRUPTED envelope has a
        blank first line) and any exception a command did not handle reach us
        here and are rendered with the same formatter the commands use.
        ``--format json`` therefore holds for *every* exit, including "no such
        option" and "no such command". A command that already
        emitted its envelope and raised ``Exit(n)`` comes back from Click as the
        integer ``n`` and is passed through untouched.
        """
        argv = list(args) if args is not None else sys.argv[1:]
        self._root_ctx = None
        exit_code = 0
        try:
            rv = super().main(
                args=argv, prog_name=prog_name, complete_var=complete_var,
                standalone_mode=False, **extra,
            )
            if isinstance(rv, int):
                exit_code = rv
        except SystemExit:
            raise
        except BaseException as exc:  # the envelope of last resort
            exit_code = self._emit_escaped_error(exc, argv)
        sys.exit(exit_code)

    def _emit_escaped_error(self, exc: BaseException, argv: list[str]) -> int:
        classified = classify(exc)
        ctx = getattr(exc, "ctx", None)
        if not isinstance(ctx, click.Context):
            ctx = self._root_ctx

        command: str | None = None
        if ctx is not None and ctx.parent is not None and ctx.command.name:
            command = ctx.command.name  # a subcommand context
        elif ctx is not None and ctx.invoked_subcommand:
            # Set by Group.invoke before the callback: authoritative once the
            # command was resolved, even though the error has only the root ctx.
            command = ctx.invoked_subcommand
        command = (
            command
            or command_name_from_argv(
                argv, is_command=_is_command, group_options=option_arity(self)
            )
            or "bootstrap"
        )

        if ctx is not None and ctx.obj:
            formatter = formatter_from_ctx(ctx)
        else:
            # The group callback never ran (unknown command, bad group option),
            # so nothing resolved the format; honour what argv asked for.
            fmt, no_color = peek_output_options(argv, group_options=option_arity(self))
            formatter = OutputFormatter(
                output_format=fmt or "table",
                backend="none",
                collection_path=None,
                no_color=no_color,
                copy_output=False,
            )
        formatter.emit_error(
            command=command,
            code=str(classified.code),
            message=classified.message,
            details=classified.details,
        )
        if debug_tracebacks_enabled() and not isinstance(exc, (click.ClickException, click.Abort)):
            traceback.print_exception(exc, file=sys.stderr)
        return int(classified.exit_code)


    def list_commands(self, ctx: click.Context) -> list[str]:
        return list_commands()

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        return get_command(cmd_name)


def _is_command(name: str) -> bool:
    return get_command(name) is not None


@click.group(
    cls=NamespaceGroup,
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(list(get_args(OutputFormat)), case_sensitive=False),
    default="table",
    show_default=True,
    help="Output format.",
)
@click.option("--col", "collection_path", type=click.Path(path_type=Path), default=None)
@click.option(
    "--backend",
    type=click.Choice(list(get_args(BackendPreference)), case_sensitive=False),
    default="auto",
    show_default=True,
)
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
            "warnings": runtime.warnings,
        }
    )

    if ctx.invoked_subcommand in _BACKENDLESS:
        ctx.obj.update(
            {
                "collection_path": runtime.collection_override,
                "backend": "none",
                "backend_reason": "not required",
            }
        )
    elif ctx.invoked_subcommand is not None:
        # Detect on first use (#32): the factory probes when the command
        # actually opens a session and writes the result back here, so the
        # HTTP probe / process scan / lock probe run once and only if needed.
        ctx.obj.update(
            {
                "collection_path": runtime.collection_override,
                "backend": runtime.backend,
                "backend_reason": DETECTION_PENDING,
            }
        )
    else:
        # Bare ``anki`` opens the REPL, which shows the backend in its header
        # and must warn up front when there is none: detect eagerly here.
        try:
            detection = detect_backend(
                forced_backend=runtime.backend,
                col_override=runtime.collection_override,
                ankiconnect_url=runtime.app.backend.ankiconnect_url,
                anki_profile=runtime.app.collection.anki_profile,
                allow_non_localhost=runtime.app.backend.allow_non_localhost,
            )
        except DetectionError as exc:
            # The REPL still opens on a host with no Anki — the warning
            # explains why, and backend commands surface the failure via the
            # backend factory. Degrade, don't exit.
            click.echo(
                f"warning: {exc} (backend commands unavailable)",
                err=True,
            )
            ctx.obj.update(
                {
                    "collection_path": runtime.collection_override,
                    "backend": "none",
                    "backend_reason": str(exc),
                }
            )
        else:
            ctx.obj.update(
                {
                    "collection_path": detection.collection_path,
                    "backend": detection.backend,
                    "backend_reason": detection.reason,
                    "ankiconnect_version": detection.ankiconnect_version,
                }
            )

    if ctx.invoked_subcommand is None:
        try:
            from anki_cli.tui.repl import run_repl
            run_repl(ctx.obj)
        except ImportError:
            click.echo(ctx.get_help())
