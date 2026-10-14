"""``--limit`` / ``--offset`` shared by the commands that list cards."""

from __future__ import annotations

import click

DEFAULT_CARD_LIMIT = 1000

limit_option = click.option(
    "--limit",
    type=int,
    default=DEFAULT_CARD_LIMIT,
    show_default=True,
    help="Max cards to return (0 = no limit); count of all matches is in `total`",
)

offset_option = click.option(
    "--offset",
    type=click.IntRange(min=0),
    default=0,
    show_default=True,
    help="Skip this many matches before returning cards (use with --limit to page)",
)


def paginate[T](items: list[T], *, limit: int, offset: int) -> list[T]:
    """``items[offset:offset+limit]``; ``limit <= 0`` means to the end."""
    end = offset + limit if limit > 0 else None
    return items[offset:end]
