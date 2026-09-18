from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from anki_cli.core.due import due_sort_key


def pick_next_due_card_id(
    backend: Any,
    *,
    deck: str | None = None,
    scan_limit: int = 200,
) -> tuple[int | None, str]:
    """
    Backend-agnostic next-card picker.

    Priority:
      1) learning due      (is:learn is:due)
      2) review due        (is:review is:due)
      3) new               (is:new)

    We scan up to scan_limit IDs and choose the one due soonest, comparing by
    the *decoded* due unit (``due_info``, see ``core.due.due_sort_key``): the
    ``is:learn is:due`` set mixes intraday cards whose ``due`` is an epoch with
    day-learn cards whose ``due`` is a day index, so a raw ``min(due)`` always
    picked the day-learn card. Fast enough for CLI usage; works on both
    backends now that both emit ``due_info``.
    """
    prefix = f'deck:"{deck}" ' if deck else ""

    categories: list[tuple[str, str]] = [
        ("learn_due", prefix + "is:learn is:due"),
        ("review_due", prefix + "is:review is:due"),
        ("new", prefix + "is:new"),
    ]

    for label, query in categories:
        ids = backend.find_cards(query=query) if query else []
        if not ids:
            continue

        best_id: int | None = None
        best_key: tuple[int, int] | None = None

        for cid in ids[: max(1, int(scan_limit))]:
            card_obj = backend.get_card(int(cid))
            card_map = cast(Mapping[str, Any], card_obj) if isinstance(card_obj, Mapping) else {}
            due_val = card_map.get("due")
            if not isinstance(due_val, int):
                continue
            due_info = card_map.get("due_info")
            key = due_sort_key(due_info if isinstance(due_info, dict) else None, fallback=due_val)

            if best_key is None or key < best_key:
                best_key = key
                best_id = int(cid)

        if best_id is not None:
            return best_id, label

    return None, "none"
