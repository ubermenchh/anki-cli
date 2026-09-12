from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast


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

    We scan up to scan_limit IDs and choose the smallest due (epoch for learning,
    day-index for review, position for new). This is fast enough for CLI usage
    and works across backends.
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
        best_due: int | None = None

        for cid in ids[: max(1, int(scan_limit))]:
            card_obj = backend.get_card(int(cid))
            card_map = cast(Mapping[str, Any], card_obj) if isinstance(card_obj, Mapping) else {}
            due_val = card_map.get("due")
            if not isinstance(due_val, int):
                continue

            if best_due is None or due_val < best_due:
                best_due = due_val
                best_id = int(cid)

        if best_id is not None:
            return best_id, label

    return None, "none"
