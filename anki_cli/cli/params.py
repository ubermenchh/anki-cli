from __future__ import annotations

from collections.abc import Sequence


def preprocess_argv(argv: Sequence[str]) -> list[str]:
    """
    Convert key=value arguments into Click-style --key value pairs.

    Example:
        anki note:add deck="A" Front="Q"
    becomes:
        anki note:add --deck "A" --Front "Q"
    """
    out: list[str] = []
    i = 0
    argv_list = list(argv)

    while i < len(argv_list):
        token = argv_list[i]

        if token == "--":
            out.append("--")
            out.extend(argv_list[i + 1 :])
            break

        if _looks_like_named_param(token):
            key, value = token.split("=", 1)
            out.append(f"--{key}")
            out.append(value)
        else:
            out.append(token)

        i += 1

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
