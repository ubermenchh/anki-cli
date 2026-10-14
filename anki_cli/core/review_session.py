"""The moving parts of a review session, shared by every front end (#36).

The review TUI, the REPL's inline reviewer and the ``review:*`` CLI commands
each used to re-implement next-card selection, the answer + undo-snapshot
dance and the undo itself. They agreed on the happy path and disagreed on the
edges. This is the one copy; the front ends keep only how they ask and how they
report.

Undo, rating previews and the scheduler's own card order need state only the
direct backend exposes (``supports_scheduler_introspection``). Without it the
session still picks cards (with the query-based approximation) and answers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from anki_cli.core.scheduler import pick_next_due_card_id
from anki_cli.core.undo import UndoItem, UndoStore, now_epoch_ms

_EASE_BY_NAME: dict[str, int] = {
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "again": 1,
    "hard": 2,
    "good": 3,
    "easy": 4,
}


def parse_ease(rating: str) -> int:
    """``again|hard|good|easy`` or ``1..4`` -> 1..4. Raises ``ValueError``."""
    ease = _EASE_BY_NAME.get(rating.strip().lower())
    if ease is None:
        raise ValueError("rating must be one of: 1,2,3,4,again,hard,good,easy")
    return ease


class IntrospectionUnavailableError(RuntimeError):
    """Undo / preview was asked of a backend that cannot snapshot card state."""


@dataclass(frozen=True, slots=True)
class NextCard:
    card_id: int | None
    kind: str


@dataclass(frozen=True, slots=True)
class AnswerOutcome:
    """``result`` is what the backend returned for the answer. ``undo_error`` is
    set when the answer committed but its undo entry could not be written."""

    result: Any
    undo_error: str | None = None


@dataclass(frozen=True, slots=True)
class UndoOutcome:
    item: UndoItem
    result: Any


class ReviewSession:
    def __init__(
        self,
        backend: Any,
        deck: str | None = None,
        *,
        undo_store: UndoStore | None = None,
    ) -> None:
        self.backend = backend
        self.deck = deck
        self.undo_store = undo_store if undo_store is not None else UndoStore()

    # -- state -------------------------------------------------------------------

    @property
    def deck(self) -> str | None:
        return self._deck

    @deck.setter
    def deck(self, value: str | None) -> None:
        self._deck = value.strip() if value and value.strip() else None

    @property
    def introspects(self) -> bool:
        """Whether undo, previews and the scheduler's card order are available."""
        return bool(getattr(self.backend, "supports_scheduler_introspection", False))

    @property
    def collection(self) -> str:
        col = getattr(self.backend, "collection_path", None)
        return str(col) if col is not None else ""

    # -- next card ---------------------------------------------------------------

    def next(self) -> NextCard:
        """The scheduler's own pick when the backend exposes it, else the
        query-based approximation. The scheduler's "nothing due" is final: the
        approximation would resurface cards the scheduler hides on purpose."""
        if self.introspects:
            picked = self.backend.get_next_due_card(self._deck)
            if not isinstance(picked, Mapping):
                return NextCard(None, "none")
            cid = picked.get("card_id")
            card_id = int(cid) if isinstance(cid, int) and cid > 0 else None
            return NextCard(card_id, str(picked.get("kind", "none")))
        card_id, kind = pick_next_due_card_id(self.backend, deck=self._deck)
        return NextCard(card_id, kind)

    # -- answer / undo / preview ---------------------------------------------------

    def answer(self, card_id: int, ease: int) -> AnswerOutcome:
        """Answer ``card_id``. The undo snapshot is taken before the answer but
        pushed only after it succeeds, so a failed answer cannot leave a stale
        entry; a failed *push* is reported, not raised (the answer is already
        committed, and retrying would apply a second review)."""
        snapshot: dict[str, Any] | None = None
        if self.introspects:
            snapshot = cast(dict[str, Any], self.backend.snapshot_card_state(int(card_id)))

        result = self.backend.answer_card(card_id=int(card_id), ease=int(ease))

        undo_error: str | None = None
        if snapshot is not None:
            # The revlog id lets undo delete exactly the row this answer wrote
            # instead of guessing from a time window.
            revlog_id = result.get("revlog_id") if isinstance(result, Mapping) else None
            item = UndoItem(
                collection=self.collection,
                card_id=int(card_id),
                snapshot={**snapshot, "revlog_id": revlog_id},
                created_at_epoch_ms=now_epoch_ms(),
            )
            try:
                self.undo_store.push(item)
            except OSError as exc:
                undo_error = str(exc)
        return AnswerOutcome(result, undo_error)

    def undo(self) -> UndoOutcome | None:
        """Restore the most recent answered card; ``None`` when there is nothing
        to undo. Raises ``IntrospectionUnavailableError`` on backends without
        snapshots."""
        if not self.introspects:
            raise IntrospectionUnavailableError("undo needs the direct backend")
        item = self.undo_store.pop(collection=self.collection)
        if item is None:
            return None
        result = self.backend.restore_card_state(item.snapshot)
        return UndoOutcome(item, result)

    def preview(self, card_id: int) -> list[Any]:
        """What each of the four ratings would do to ``card_id``."""
        if not self.introspects:
            raise IntrospectionUnavailableError("rating previews need the direct backend")
        return list(self.backend.preview_ratings(int(card_id)))
