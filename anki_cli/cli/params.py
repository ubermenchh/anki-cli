from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import click

# For each option spelling (``--deck``, ``-d``), how many following argv tokens
# it consumes as its value: 0 for flags and count options, ``nargs`` otherwise.
OptionArity = Mapping[str, int]

# Click adds these outside ``command.params``; they never consume a token.
_HELP_SPELLINGS: dict[str, int] = {"-h": 0, "--help": 0}


class DanglingOptionError(ValueError):
    """A group option written after the subcommand has no value token left
    (``anki note:delete --id 1 --format``). Hoisting it would make Click read
    the *command name* as its value, so the caller reports it instead."""

    def __init__(self, option: str) -> None:
        self.option = option
        super().__init__(f"Option '{option}' requires an argument.")


def option_arity(command: click.Command | None) -> OptionArity:
    """Option spellings of ``command`` and how many value tokens each consumes."""
    if command is None:
        return {}
    out: dict[str, int] = dict(_HELP_SPELLINGS)
    for param in command.params:
        if not isinstance(param, click.Option):
            continue
        consumes = 0 if (param.is_flag or param.count) else max(int(param.nargs), 0)
        for spelling in (*param.opts, *param.secondary_opts):
            out[spelling] = consumes
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
    tokens after it (group options stay known for their arity). The CLI group
    passes both; the REPL passes the resolver.

    Option spellings not in the table (a typo, or a group option placed after
    the subcommand, see #26) are assumed to consume one token, so the user's
    intended value is never rewritten; Click reports the unknown option itself.
    Without any table this rule still applies to every option, which is the
    only behavioural difference from the original all-tokens-are-sugar pass.
    """
    out: list[str] = []
    argv_list = list(argv)
    group: dict[str, int] = dict(group_options or {})
    options: dict[str, int] = dict(group)
    command_seen = resolve_command_options is None
    # How many upcoming tokens belong to the option just seen.
    pending_values = 0

    for i, token in enumerate(argv_list):
        if token == "--":
            out.append("--")
            out.extend(argv_list[i + 1 :])
            break

        if pending_values > 0:
            out.append(token)
            pending_values -= 1
            continue

        if token.startswith("-") and token != "-":
            out.append(token)
            pending_values = _tokens_consumed_by(token, options)
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


def _tokens_consumed_by(token: str, options: Mapping[str, int]) -> int:
    """Following tokens Click will treat as this option's value(s)."""
    if token.startswith("--"):
        if "=" in token:
            return 0  # --opt=value carries its own value
        return options.get(token, 1)
    # Short option: -q, -qVALUE (attached value) or -abc (flag cluster).
    if len(token) == 2:
        return options.get(token, 1)
    head = token[:2]
    if options.get(head, 1) > 0:
        return 0  # -qVALUE: the value is attached
    # Flag cluster: only the last flag can still want a value.
    return options.get(f"-{token[-1]}", 1)


def _looks_like_named_param(token: str) -> bool:
    if "=" not in token:
        return False
    if token.startswith("-"):
        return False

    key, _ = token.split("=", 1)
    if not key:
        return False

    return not any(ch.isspace() for ch in key)


def hoist_group_options(
    argv: Sequence[str],
    *,
    group_options: OptionArity,
    is_command: Callable[[str], bool],
    resolve_command_options: Callable[[str], OptionArity | None],
    keep_in_place: frozenset[str] = frozenset({*_HELP_SPELLINGS, "--version"}),
) -> list[str]:
    """Move group options written after the subcommand to before it (#26).

    ``anki note:delete --id 1 --yes`` becomes ``anki --yes note:delete --id 1``.
    Click only accepts a group's options before the subcommand name; every
    documented example (and every agent following them) puts ``--yes`` /
    ``--format`` last. A spelling the subcommand itself defines is left alone
    (the subcommand wins), as are ``-h/--help`` and ``--version`` (they answer
    for the subcommand, not the group) and anything after ``--``.

    Runs on argv that ``preprocess_argv`` has already normalised.
    """
    tokens = list(argv)
    pending = 0
    command_index: int | None = None
    for i, token in enumerate(tokens):
        if token == "--":
            return tokens
        if pending > 0:
            pending -= 1
            continue
        if token.startswith("-") and token != "-":
            pending = _tokens_consumed_by(token, group_options)
            continue
        if is_command(token):
            command_index = i
        break

    if command_index is None:
        return tokens

    command_options = resolve_command_options(tokens[command_index]) or {}
    head = tokens[: command_index + 1]
    hoisted: list[str] = []
    rest: list[str] = []
    tail = tokens[command_index + 1 :]
    i = 0
    while i < len(tail):
        token = tail[i]
        if token == "--":
            rest.extend(tail[i:])
            break
        spelling = token.split("=", 1)[0] if token.startswith("--") else token
        is_group_option = (
            spelling in group_options
            and spelling not in command_options
            and spelling not in keep_in_place
        )
        if not is_group_option:
            consumed = 0
            if token.startswith("-") and token != "-":
                consumed = _tokens_consumed_by(token, command_options)
                # A spelling the subcommand does not define is a typo Click will
                # reject; never let it swallow a group option that follows it,
                # or the usage error loses the caller's --format.
                if spelling not in command_options and i + 1 < len(tail):
                    nxt = tail[i + 1]
                    nxt_spelling = nxt.split("=", 1)[0] if nxt.startswith("--") else nxt
                    if (
                        nxt_spelling in group_options
                        and nxt_spelling not in command_options
                        and nxt_spelling not in keep_in_place
                    ):
                        consumed = 0
            rest.extend(tail[i : i + 1 + consumed])
            i += 1 + consumed
            continue
        consumed = _tokens_consumed_by(token, group_options)
        if i + consumed >= len(tail):
            raise DanglingOptionError(spelling)
        hoisted.extend(tail[i : i + 1 + consumed])
        i += 1 + consumed

    if not hoisted:
        return tokens
    return [*head[:-1], *hoisted, head[-1], *rest]


def command_name_from_argv(
    argv: Sequence[str],
    *,
    is_command: Callable[[str], bool],
    group_options: OptionArity,
) -> str | None:
    """The command the caller was trying to run: the first positional after the
    group options (and their values, by arity). ``None`` if that positional is
    not a registered command — ``anki nope probe`` is an error about ``nope``,
    not a run of ``probe`` — or there is none before ``--``."""
    tokens = list(argv)
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "--":
            return None
        if token.startswith("-") and token != "-":
            i += 1 + _tokens_consumed_by(token, group_options)
            continue
        return token if is_command(token) else None
    return None


def peek_output_options(
    argv: Sequence[str], *, group_options: OptionArity
) -> tuple[str | None, bool]:
    """``(--format value, --no-color present)`` read straight off argv.

    Used when an error fires before the group callback has built ``ctx.obj``
    (unknown command, bad group option): the envelope should still honour the
    format the caller asked for. Tracks option arity so ``--col --format`` reads
    ``--format`` as ``--col``'s value, exactly as Click will. The format is
    lowercased like the ``--format`` option (``case_sensitive=False``). Stops
    at ``--``.
    """
    fmt: str | None = None
    no_color = False
    tokens = list(argv)
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "--":
            break
        if token.startswith("--format="):
            fmt = token.split("=", 1)[1]
            i += 1
            continue
        if token == "--format":
            if i + 1 < len(tokens):
                fmt = tokens[i + 1]
            i += 2
            continue
        if token == "--no-color":
            no_color = True
            i += 1
            continue
        if token.startswith("-") and token != "-":
            i += 1 + _tokens_consumed_by(token, group_options)
            continue
        i += 1
    return (fmt.lower() if fmt else None), no_color
