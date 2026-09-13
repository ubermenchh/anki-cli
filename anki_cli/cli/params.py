from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import click

# For each option spelling (``--deck``, ``-d``), how many following argv tokens
# it consumes as its value: 0 for flags and count options, ``nargs`` otherwise.
OptionArity = Mapping[str, int]

# Click adds these outside ``command.params``; they never consume a token.
_HELP_SPELLINGS: dict[str, int] = {"-h": 0, "--help": 0}


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
