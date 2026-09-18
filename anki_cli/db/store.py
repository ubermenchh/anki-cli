"""The direct SQLite store: one class assembled from the per-area mixins.

Layering (each mixin extends the ones it calls into, so ``self.`` cross-calls
are checked, not assumed)::

    CodecMixin
      ConnectionMixin          open, write txn, ids, timing, col bookkeeping
        DeckLookupMixin        name -> id, subtree filter, deck options (deck_lookup.py)
          NotetypesMixin       notetypes (+ flds rewrites, scm bump)
          CardsMixin           cards, due counts, queue mutators, revlog
            DecksMixin         decks public API (needs due counts)
            SchedulingMixin    FSRS answer/preview/snapshot
          NotesMixin           notes + tags (needs notetypes, deck lookup)
"""

from __future__ import annotations

from anki_cli.db.decks import DecksMixin
from anki_cli.db.notes import NotesMixin
from anki_cli.db.scheduling import SchedulingMixin


class AnkiDirectStore(DecksMixin, NotesMixin, SchedulingMixin):
    """Read and write Anki's collection (``.anki2``/``.anki21b``) directly."""
