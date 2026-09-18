"""``@anki_command``: the scaffolding every CLI command used to copy by hand (#29).

Before, each handler read ``ctx.obj``, built a formatter, opened the backend
session, caught ``(BackendFactoryError, NotImplementedError)`` for
``BACKEND_UNAVAILABLE``, and called ``emit_success`` — 50-odd near-identical
copies. The decorator owns all of that. What stays with the command is the part
that genuinely differs per command: which *other* exceptions it turns into which
envelope code, and the ``details`` it attaches (those are pinned by tests and
differ command to command; a single global table would change behaviour, which
this refactor must not).

The session factory and formatter are resolved via their modules
(``anki_cli.backends.factory``, ``anki_cli.cli.formatter``); tests patch there.

Usage::

    @anki_command("deck", errors={LookupError: ("ENTITY_NOT_FOUND", 4)})
    @click.option("--deck", "deck_name", required=True)
    def deck_cmd(cmd: CommandContext, deck_name: str) -> JSONValue:
        name = deck_name.strip()
        if not name:
            raise cmd.invalid("Deck name cannot be empty.")
        with cmd.errors(details={"deck": name}):
            return cmd.backend.get_deck(name)

The wrapped function returns the ``data`` for the success envelope, or ``None``
to emit nothing (TUI launchers). Anything it raises that is not mapped falls
through to the entry-point mapper from #27, which renders an envelope too — so
no failure escapes as a traceback either way.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from typing import Any

import click

from anki_cli.backends import factory
from anki_cli.backends.ankiconnect import AnkiConnectAPIError
from anki_cli.backends.factory import BackendFactoryError
from anki_cli.cli import formatter as formatter_mod
from anki_cli.cli.dispatcher import register_command
from anki_cli.db.search_sql import SearchParseError
from anki_cli.models.output import JSONValue

# exception type -> (error code, exit status)
ErrorMap = Mapping[type[BaseException], tuple[str, int]]

BACKEND_UNAVAILABLE: tuple[str, int] = ("BACKEND_UNAVAILABLE", 7)


class CommandExit(click.exceptions.Exit):
    """An envelope has been emitted; unwind with this exit status.

    Subclasses ``click.exceptions.Exit`` so the #27 entry point passes the
    status through untouched instead of wrapping a second envelope.
    """


class CommandContext:
    """Everything a command body needs, resolved lazily.

    ``backend`` opens the session on first access and closes it when the
    command finishes. Both the session factory and the formatter are looked up
    through their modules at call time (``factory.backend_session_from_context``,
    ``formatter_mod.formatter_from_ctx``), which is where tests patch them.
    """

    def __init__(self, *, name: str, ctx: click.Context, errors: ErrorMap) -> None:
        self.name = name
        self.ctx = ctx
        self._default_errors: ErrorMap = errors
        self.obj: dict[str, Any] = ctx.obj or {}
        self.formatter = formatter_mod.formatter_from_ctx(ctx)
        self.warnings: list[str] = []
        self._stack = ExitStack()
        self._backend: Any = None

    # -- backend session -------------------------------------------------------------

    @property
    def backend(self) -> Any:
        if self._backend is None:
            session = factory.backend_session_from_context(self.obj)
            self._backend = self._stack.enter_context(session)
        return self._backend

    def close(self) -> None:
        self._stack.close()

    # -- failing ---------------------------------------------------------------------

    def fail(
        self,
        code: str,
        message: str,
        *,
        exit_code: int,
        details: Mapping[str, JSONValue] | None = None,
    ) -> CommandExit:
        """Emit an error envelope and return the exception to ``raise``."""
        self.formatter.emit_error(
            command=self.name,
            code=code,
            message=message,
            details=dict(details) if details else None,
        )
        return CommandExit(exit_code)

    def invalid(
        self, message: str, *, details: Mapping[str, JSONValue] | None = None
    ) -> CommandExit:
        return self.fail("INVALID_INPUT", message, exit_code=2, details=details)

    def invalid_query(self, query: str | None, exc: BaseException) -> CommandExit:
        """``INVALID_INPUT`` for a search the backend rejected, with the
        parser position when there is one."""
        details: dict[str, JSONValue] = {"query": query or ""}
        if isinstance(exc, SearchParseError) and exc.position is not None:
            details["position"] = exc.position
        return self.fail(
            "INVALID_INPUT", f"Invalid search query: {exc}", exit_code=2, details=details
        )

    @contextmanager
    def query_errors(self, query: str | None, *, ankiconnect: bool = True) -> Iterator[None]:
        """Map a rejected search to ``invalid_query``.

        ``SearchParseError`` (direct) always; ``AnkiConnectAPIError`` only when
        ``ankiconnect`` is set — the read-only listers treat any AnkiConnect
        error on ``find_*`` as a bad query, while the mutating commands leave it
        to their own table (an ``addTags`` failure is not a query problem).
        Use around the ``find_*`` call, ahead of the command's own table.
        """
        try:
            yield
        except SearchParseError as exc:
            raise self.invalid_query(query, exc) from exc
        except AnkiConnectAPIError as exc:
            if not ankiconnect:
                raise
            raise self.invalid_query(query, exc) from exc

    def require_ids(self, entity_id: int | None, query: str | None) -> None:
        if entity_id is None and not query:
            raise self.invalid("Provide --id or --query.")

    def require_yes(self, message: str, *, details: Mapping[str, JSONValue] | None = None) -> None:
        """``CONFIRMATION_REQUIRED`` unless ``--yes`` was given."""
        if not bool(self.obj.get("yes", False)):
            merged: dict[str, JSONValue] = {**(details or {}), "hint": "Re-run with --yes."}
            raise self.fail("CONFIRMATION_REQUIRED", message, exit_code=2, details=merged)

    def backend_unavailable(self, exc: BaseException) -> CommandExit:
        code, exit_code = BACKEND_UNAVAILABLE
        return self.fail(
            code,
            str(exc),
            exit_code=exit_code,
            details={"backend": str(self.obj.get("backend", "unknown"))},
        )

    @contextmanager
    def errors(self, *, details: Mapping[str, JSONValue] | None = None) -> Iterator[None]:
        """Turn the exceptions in the command's table into envelopes, attaching
        ``details``. Backend-unavailable is always handled first. Anything not
        in the table propagates to the entry-point mapper (#27)."""
        table = self._default_errors
        try:
            yield
        except CommandExit:
            raise
        except Exception as exc:
            # Backend-unavailable is decided *before* the table: the old
            # handlers listed that clause first, and both of these types are
            # RuntimeError subclasses, so a table row for RuntimeError (note:bulk)
            # would otherwise capture them. A command may still override by
            # naming the exact type.
            if (
                isinstance(exc, (BackendFactoryError, NotImplementedError))
                and type(exc) not in table
            ):
                raise self.backend_unavailable(exc) from exc
            mapped = _mapped_by_mro(exc, table)
            if mapped is None:
                raise
            code, exit_code = mapped
            raise self.fail(code, str(exc), exit_code=exit_code, details=details) from exc


def _mapped_by_mro(exc: BaseException, table: ErrorMap) -> tuple[str, int] | None:
    """First table entry in ``type(exc).__mro__`` — a subclass entry beats its
    base regardless of dict order, and the *most specific* match wins, which is
    what the old ``except`` clauses did when listed specific-first."""
    for klass in type(exc).__mro__:
        if issubclass(klass, BaseException):
            hit = table.get(klass)
            if hit is not None:
                return hit
    return None


def anki_command(
    name: str,
    *,
    errors: ErrorMap | None = None,
    register: bool = True,
    **command_kwargs: Any,
) -> Callable[[Callable[..., Any]], click.Command]:
    """Declare a CLI command.

    ``errors`` is the command's exception table, applied by ``cmd.errors()``
    blocks in the body (and to the body as a whole). Everything the body
    returns is the success ``data``; ``None`` emits nothing.
    """
    table: ErrorMap = dict(errors or {})

    def decorate(fn: Callable[..., Any]) -> click.Command:
        @functools.wraps(fn)
        @click.pass_context
        def runner(ctx: click.Context, /, **kwargs: Any) -> None:
            cmd = CommandContext(name=name, ctx=ctx, errors=table)
            try:
                with cmd.errors():
                    data = fn(cmd, **kwargs)
            finally:
                cmd.close()
            if data is None:
                return
            if cmd.warnings:
                cmd.formatter.emit_success(command=name, data=data, warnings=cmd.warnings)
            else:
                cmd.formatter.emit_success(command=name, data=data)

        command = click.command(name, **command_kwargs)(runner)
        if register:
            register_command(name, command)
        return command

    return decorate


def id_or_query(entity: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """The ``--id`` / ``--query`` pair most mutating commands take."""
    id_dest = f"{entity}_id"

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn = click.option("--query", default=None, help=f"Search query for target {entity}s")(fn)
        fn = click.option("--id", id_dest, type=int, default=None, help=f"{entity.title()} ID")(fn)
        return fn

    return decorate


__all__ = [
    "BACKEND_UNAVAILABLE",
    "CommandContext",
    "CommandExit",
    "ErrorMap",
    "anki_command",
    "id_or_query",
]
