from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import click

# For each option spelling (``--deck``, ``-d``), whether it consumes the next argv
# token as its value. Flags map to False.
OptionArity = Mapping[str, bool]


def option_arity(command: click.Command | None) -> OptionArity:
    """Option spellings of ``command`` and whether each takes a value."""
    if command is None:
        return {}
    out: dict[str, bool] = {}
    for param in command.params:
        if not isinstance(param, click.Option):
            continue
        takes_value = not param.is_flag and param.nargs != 0
        for spelling in (*param.opts, *param.secondary_opts):
            out[spelling] = takes_value
    return out


def preprocess_argv(
    argv: Sequence[str],
    *,
    group_options: OptionArity | None = None,
    resolve_command_options: Callable[[str], OptionArity | None] | None = None,
) -> list[str]:
    """Convert ``key=value`` sugar into Click-style ``--key value`` pairs.

    Example::

        anki note:add deck="A" Front="Q"   ->   anki note:add --deck "A" --Front "Q"

    A token is only rewritten when it is *not* the value of a preceding option
    (``--query "prop:lapses=0"`` must reach Click untouched). Deciding that needs
    the option table of whatever command is in effect: ``group_options`` covers
    tokens before the subcommand name, ``resolve_command_options(name)`` covers
    tokens after it. When neither is supplied every token is a candidate, which
    is only safe for argv that contains no option values (the REPL passes the
    resolver; the CLI group passes both).

    Unknown option spellings are assumed to take a value, so a typo never causes
    the next token to be mangled; Click reports the unknown option itself.
    """
    out: list[str] = []
    argv_list = list(argv)
    group: dict[str, bool] = dict(group_options or {})
    options: dict[str, bool] = dict(group)
    command_seen = resolve_command_options is None
    # Track whether the *previous* token was an option that consumes this one.
    expecting_value = False

    for i, token in enumerate(argv_list):
        if token == "--":
            out.append("--")
            out.extend(argv_list[i + 1 :])
            break

        if expecting_value:
            out.append(token)
            expecting_value = False
            continue

        if token.startswith("-") and token != "-":
            out.append(token)
            spelling = token.split("=", 1)[0]
            # --opt=value carries its own value. Otherwise consult the option
            # table; unknown spellings (a typo, or a group option placed after
            # the subcommand, see #26) are assumed to consume a value so the
            # user's intended value is never rewritten.
            expecting_value = False if "=" in token else options.get(spelling, True)
            continue

        if not command_seen and resolve_command_options is not None:
            resolved = resolve_command_options(token)
            if resolved is not None:
                command_seen = True
                # Group options stay known so their arity is still honoured.
                options = {**group, **resolved}
                out.append(token)
                continue

        if _looks_like_named_param(token):
            key, value = token.split("=", 1)
            out.append(f"--{key}")
            out.append(value)
        else:
            out.append(token)

    return out


def _looks_like_named_param(token: str) -> bool:
    if "=" not in token:
        return False
    if token.startswith("-"):
        return False

    key, _ = token.split("=", 1)
    if not key:
        return False

    return not any(ch.isspace() for ch in key)
