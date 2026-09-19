"""FSRS scheduling: ``answer_card``, ``preview_ratings``, undo snapshots, and
the mapping between Anki's card columns and ``py-fsrs`` state.
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple, TypedDict, cast

from fsrs import Card as FSRSCard
from fsrs import Rating, ReviewLog, Scheduler, State
from fsrs.scheduler import LOWER_BOUNDS_PARAMETERS, UPPER_BOUNDS_PARAMETERS

from anki_cli.core.due import is_intraday_learn_due
from anki_cli.db.cards import LEAVE_FILTERED_DECK_SQL, CardsMixin
from anki_cli.db.timing import SchedTiming
from anki_cli.models.output import JSONValue
from anki_cli.proto.anki.deck_config import DeckConfigConfig


class _SchedulerStepKwargs(TypedDict, total=False):
    """Optional step overrides for ``fsrs.Scheduler`` (PEP 692 ``**`` kwargs).

    Keeping this a TypedDict lets ``ty`` check each key against the matching
    ``Scheduler.__init__`` parameter instead of treating the unpacked values as
    an opaque ``dict[str, list[timedelta]]``.
    """

    learning_steps: list[timedelta]
    relearning_steps: list[timedelta]


class _FsrsReviewSetup(NamedTuple):
    """What ``_prepare_fsrs_review`` hands to ``answer_card`` / ``preview_ratings``."""

    card: FSRSCard
    scheduler: Scheduler
    timing: SchedTiming
    desired_retention: float
    learn_step_count: int
    relearn_step_count: int
    params_source: str


# py-fsrs 6 wants exactly 21 weights. Anki may still carry FSRS-4.5 (17) or
# FSRS-5 (19) weights from an older optimizer run; fsrs-rs upgrades those with
# fixed transforms (model_v6::check_and_fill_parameters_fsrs6), mirrored here.
FSRS6_PARAM_COUNT = 21
FSRS5_DEFAULT_DECAY = 0.5
FSRS6_DEFAULT_PARAMETERS: tuple[float, ...] = (
    0.212, 1.2931, 2.3065, 8.2956, 6.4133, 0.8334, 3.0194, 0.001, 1.8722, 0.1666, 0.796,
    1.4835, 0.0614, 0.2629, 1.6483, 0.6014, 1.8729, 0.5425, 0.0912, 0.0658, 0.1542,
)

# Anki's revlog.type (RevlogReviewKind).
REVLOG_KIND_LEARNING = 0
REVLOG_KIND_REVIEW = 1
REVLOG_KIND_RELEARNING = 2
REVLOG_KIND_FILTERED = 3  # also used for a review answered before it was due


def upgrade_fsrs_parameters(values: list[float]) -> tuple[list[float], str]:
    """Return 21 FSRS-6 weights plus a label describing where they came from.

    Port of fsrs-rs ``check_and_fill_parameters_fsrs6``: 17 (FSRS-4.5) and 19
    (FSRS-5) weight sets are transformed the way Anki transforms them before
    scheduling; anything else falls back to the FSRS-6 defaults.
    """
    n = len(values)
    if n == FSRS6_PARAM_COUNT:
        return list(values), "fsrs6"
    if n == 19:
        return [*values, 0.0, FSRS5_DEFAULT_DECAY], "fsrs5-upgraded"
    if n == 17:
        w = list(values)
        if w[5] * 3.0 + 1.0 <= 0.0:
            # Corrupt blob; the log below would raise. Anki-optimized w5 >= 0.1.
            return list(FSRS6_DEFAULT_PARAMETERS), "default"
        w[4] = w[5] * 2.0 + w[4]
        w[5] = math.log(w[5] * 3.0 + 1.0) / 3.0
        w[6] += 0.5
        return [*w, 0.0, 0.0, 0.0, FSRS5_DEFAULT_DECAY], "fsrs4.5-upgraded"
    return list(FSRS6_DEFAULT_PARAMETERS), "default"


def clamp_fsrs_parameters(values: list[float]) -> tuple[list[float], bool]:
    """Clamp 21 weights into py-fsrs's accepted range (fsrs-rs ``parameter_clipper``).

    Anki schedules with upgraded legacy weights as-is; py-fsrs refuses anything
    out of bounds, so clamping keeps the user's optimized weights instead of
    throwing the whole set away. Returns the clamped list and whether anything
    changed.
    """
    clamped = [
        min(max(float(w), float(lo)), float(hi))
        for w, lo, hi in zip(values, LOWER_BOUNDS_PARAMETERS, UPPER_BOUNDS_PARAMETERS, strict=True)
    ]
    return clamped, clamped != [float(w) for w in values]


def fsrs_fuzz_seed(card_id: int, reps: int) -> int:
    """rslib ``get_fuzz_seed_for_id_and_reps``: the same card at the same rep
    count always fuzzes the same way, so a preview matches the later answer."""
    return (int(card_id) + int(reps)) & 0xFFFFFFFFFFFFFFFF


class SchedulingMixin(CardsMixin):
    """Public scheduling API."""

    def snapshot_card_state(self, card_id: int) -> dict[str, JSONValue]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    id, did, odid, odue, ord, type, queue, due, ivl, factor, reps, lapses,
                    left, flags, data
                FROM cards
                WHERE id = ?
                """,
                (card_id,),
            ).fetchone()

        if row is None:
            raise LookupError(f"Card not found: {card_id}")

        return {
            "id": int(row["id"]),
            "did": int(row["did"]),
            "odid": int(row["odid"]),
            "odue": int(row["odue"]),
            "ord": int(row["ord"]),
            "type": int(row["type"]),
            "queue": int(row["queue"]),
            "due": int(row["due"]),
            "ivl": int(row["ivl"]),
            "factor": int(row["factor"]),
            "reps": int(row["reps"]),
            "lapses": int(row["lapses"]),
            "left": int(row["left"]),
            "flags": int(row["flags"]),
            "data": str(row["data"] or ""),
        }

    def restore_card_state(self, snapshot: Mapping[str, Any]) -> dict[str, JSONValue]:
        card_id = snapshot.get("id")
        if not isinstance(card_id, int):
            raise ValueError("snapshot.id must be an int")

        now_sec = int(time.time())
        with self._connect_write() as conn:
            updated = conn.execute(
                """
                UPDATE cards
                SET
                    did = ?,
                    odid = ?,
                    odue = ?,
                    ord = ?,
                    type = ?,
                    queue = ?,
                    due = ?,
                    ivl = ?,
                    factor = ?,
                    reps = ?,
                    lapses = ?,
                    left = ?,
                    flags = ?,
                    data = ?,
                    mod = ?,
                    usn = -1
                WHERE id = ?
                """,
                (
                    int(snapshot.get("did") or 0),
                    # Older snapshots predate these keys; a card that was not in a
                    # filtered deck has both at 0.
                    int(snapshot.get("odid") or 0),
                    int(snapshot.get("odue") or 0),
                    int(snapshot.get("ord") or 0),
                    int(snapshot.get("type") or 0),
                    int(snapshot.get("queue") or 0),
                    int(snapshot.get("due") or 0),
                    int(snapshot.get("ivl") or 0),
                    int(snapshot.get("factor") or 0),
                    int(snapshot.get("reps") or 0),
                    int(snapshot.get("lapses") or 0),
                    int(snapshot.get("left") or 0),
                    int(snapshot.get("flags") or 0),
                    str(snapshot.get("data") or ""),
                    now_sec,
                    card_id,
                ),
            ).rowcount

            # Match Anki's undo semantics: delete the exact revlog row the
            # undone review wrote (its id is recorded in the snapshot when the
            # undo entry is pushed) instead of appending a compensating row.
            # Without this the review's own ease 1..4 row survives and
            # _seed_fsrs_card_from_revlog keeps picking it up, so legacy cards
            # seeded from revlog compute a different stability afterwards.
            # usn = -1 restricts the delete to rows that have not synced yet;
            # revlog deletions never propagate to AnkiWeb, so removing a
            # synced row would diverge this collection from the server.
            revlog_id = snapshot.get("revlog_id")
            revlog_deleted = 0
            if isinstance(revlog_id, int) and not isinstance(revlog_id, bool):
                revlog_deleted = conn.execute(
                    "DELETE FROM revlog WHERE id = ? AND cid = ? AND usn = -1",
                    (revlog_id, card_id),
                ).rowcount

        return {
            "card_id": card_id,
            "restored": int(updated) > 0,
            "revlog_deleted": int(revlog_deleted),
        }

    def preview_ratings(self, card_id: int) -> list[dict[str, JSONValue]]:
        review_dt = datetime.now(UTC)

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    id, nid, did, ord, mod, usn, type, queue, due, ivl, factor, reps,
                    lapses, left, odue, odid, flags, data
                FROM cards
                WHERE id = ?
                """,
                (card_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"Card not found: {card_id}")

            setup = self._prepare_fsrs_review(conn, row, review_dt)
            base, scheduler, timing = setup.card, setup.scheduler, setup.timing
            learn_count, relearn_count = setup.learn_step_count, setup.relearn_step_count
            params_source = setup.params_source

            out: list[dict[str, JSONValue]] = []
            for ease in (1, 2, 3, 4):
                # Same seed answer_card will use, so the preview matches the write.
                next_card = self._review_with_fuzz_seed(
                    scheduler,
                    base,
                    Rating(ease),
                    review_datetime=review_dt,
                    card_id=int(row["id"]),
                    reps=int(row["reps"]),
                )

                (
                    new_type,
                    new_queue,
                    new_due,
                    new_ivl,
                    new_left,
                    next_due_epoch,
                ) = self._map_fsrs_result_to_anki(
                    current_row=row,
                    next_card=next_card,
                    timing=timing,
                    learn_step_count=learn_count,
                    relearn_step_count=relearn_count,
                    now_dt=review_dt,
                )

                out.append(
                    {
                        "ease": ease,
                        "fsrs_params": params_source,
                        "type": new_type,
                        "queue": new_queue,
                        "due": new_due,
                        "interval": new_ivl,
                        "left": new_left,
                        "next_due_epoch_secs": next_due_epoch,
                        "due_info": self._decode_due(
                            card_type=new_type,
                            queue=new_queue,
                            due_raw=new_due,
                            timing=timing,
                        ),
                        "state": str(next_card.state),
                    }
                )

            return out

    def answer_card(self, card_id: int, ease: int) -> dict[str, JSONValue]:
        if ease not in {1, 2, 3, 4}:
            raise ValueError("ease must be one of 1, 2, 3, 4")

        with self._connect_write() as conn:
            row = conn.execute(
                """
                SELECT
                    id, nid, did, ord, mod, usn, type, queue, due, ivl, factor, reps,
                    lapses, left, odue, odid, flags, data
                FROM cards
                WHERE id = ?
                """,
                (card_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"Card not found: {card_id}")

            review_dt = datetime.now(UTC)
            setup = self._prepare_fsrs_review(conn, row, review_dt)
            fsrs_card, scheduler, timing = setup.card, setup.scheduler, setup.timing
            learn_count, relearn_count = setup.learn_step_count, setup.relearn_step_count
            desired_retention, params_source = setup.desired_retention, setup.params_source

            next_card = self._review_with_fuzz_seed(
                scheduler,
                fsrs_card,
                Rating(ease),
                review_datetime=review_dt,
                card_id=int(row["id"]),
                reps=int(row["reps"]),
            )

            (
                new_type,
                new_queue,
                new_due,
                new_ivl,
                new_left,
                next_due_epoch,
            ) = self._map_fsrs_result_to_anki(
                current_row=row,
                next_card=next_card,
                timing=timing,
                learn_step_count=learn_count,
                relearn_step_count=relearn_count,
                now_dt=review_dt,
            )

            now_sec = int(review_dt.timestamp())
            today_days = timing.days_elapsed
            reps = int(row["reps"]) + 1
            lapses = int(row["lapses"]) + (1 if ease == 1 else 0)
            raw_data = self._parse_card_data(str(row["data"] or ""))
            data_obj = dict(raw_data) if isinstance(raw_data, dict) else {}
            # rslib stores the original new-queue position when a card leaves New;
            # for a card on loan that is odue, not the filtered-deck slot.
            data_obj.setdefault(
                "pos", max(0, self._scheduling_due(row)) if int(row["type"]) == 0 else 0
            )
            # rslib CardData.last_review_time ("lrt", seconds); Anki prefers it over
            # the revlog when computing elapsed days.
            data_obj["lrt"] = now_sec
            data_obj["dr"] = round(desired_retention, 2)
            if next_card.stability is not None:
                data_obj["s"] = round(float(next_card.stability), 4)
            if next_card.difficulty is not None:
                data_obj["d"] = round(float(next_card.difficulty), 3)

            data_json = json.dumps(data_obj, separators=(",", ":"))

            conn.execute(
                f"""
                UPDATE cards
                SET
                    mod = ?,
                    usn = -1,
                    type = ?,
                    queue = ?,
                    due = ?,
                    ivl = ?,
                    reps = ?,
                    lapses = ?,
                    left = ?,
                    data = ?,
                    {LEAVE_FILTERED_DECK_SQL}
                WHERE id = ?
                """,
                (
                    now_sec,
                    new_type,
                    new_queue,
                    new_due,
                    new_ivl,
                    reps,
                    lapses,
                    new_left,
                    data_json,
                    card_id,
                ),
            )

            revlog_id = self._allocate_row_id(conn, "revlog")
            old_due = self._scheduling_due(row)
            old_type = int(row["type"])
            old_queue = int(row["queue"])
            old_ivl = int(row["ivl"])

            # rslib as_revlog_interval: review/day-learn intervals are logged
            # in positive days, intraday learning in negative seconds.
            if new_queue == 2:
                logged_ivl = new_ivl
            elif new_queue == 3:
                logged_ivl = max(1, int(new_due - today_days))
            else:
                logged_ivl = -max(1, int(next_due_epoch - now_sec))
            if old_queue == 1:
                logged_last_ivl = -max(1, int(old_due - now_sec))
            elif old_queue == 3:
                logged_last_ivl = max(1, int(old_due - today_days))
            elif old_queue == 2:
                logged_last_ivl = max(1, old_ivl)
            else:
                logged_last_ivl = old_ivl

            if next_card.difficulty is None:
                logged_factor = int(row["factor"])
            else:
                logged_factor = max(100, min(1100, round(float(next_card.difficulty) * 100)))

            # rslib RevlogReviewKind comes from the card's state *before* the
            # answer: new/learning -> Learning, relearning -> Relearning, review ->
            # Review, or Filtered when a review is answered ahead of its due day.
            if old_type == 3:
                review_type = REVLOG_KIND_RELEARNING
            elif old_type == 2:
                review_type = (
                    REVLOG_KIND_FILTERED if old_due > today_days else REVLOG_KIND_REVIEW
                )
            else:
                review_type = REVLOG_KIND_LEARNING

            conn.execute(
                """
                INSERT INTO revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, type)
                VALUES (?, ?, -1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revlog_id,
                    card_id,
                    ease,
                    logged_ivl,
                    logged_last_ivl,
                    logged_factor,
                    0,
                    review_type,
                ),
            )

        return {
            "card_id": card_id,
            "ease": ease,
            "answered": True,
            "fsrs_params": params_source,
            "queue": new_queue,
            "type": new_type,
            "due": new_due,
            "interval": new_ivl,
            "revlog_id": revlog_id,
        }

    def _prepare_fsrs_review(
        self, conn: sqlite3.Connection, row: sqlite3.Row, review_dt: datetime
    ) -> _FsrsReviewSetup:
        """Everything ``answer_card`` and ``preview_ratings`` share before the
        rating is applied: the day timing, the home deck's scheduler, and the
        card as py-fsrs sees it (seeded from the revlog or the legacy
        ivl/factor columns when Anki never stored FSRS memory state).

        A card in a filtered deck keeps its real schedule in ``odue`` and its
        options come from the home deck. Anki (v3) answers it, sends it home,
        then schedules normally; preview decks don't reschedule at all, which
        this backend does not emulate.
        """
        timing = self._timing(conn, int(review_dt.timestamp()))

        odid = int(row["odid"])
        home_did = odid if odid != 0 else int(row["did"])
        if odid != 0 and self._filtered_deck_reschedules(conn, int(row["did"])) is False:
            raise ValueError(
                "Card is in a preview (non-rescheduling) filtered deck; "
                "answer it in Anki or empty the deck first."
            )

        (
            scheduler,
            desired_retention,
            learn_count,
            relearn_count,
            params_source,
        ) = self._build_scheduler_ex(conn, home_did)

        fsrs_card = self._card_row_to_fsrs(
            row,
            timing=timing,
            now_dt=review_dt,
            learn_step_count=learn_count,
            relearn_step_count=relearn_count,
        )
        if fsrs_card.last_review is None:
            fsrs_card.last_review = self._last_review_time(conn, int(row["id"]))

        needs_seed = fsrs_card.state in (State.Review, State.Relearning) and (
            fsrs_card.stability is None
            or fsrs_card.difficulty is None
            or fsrs_card.last_review is None
        )
        if needs_seed:
            seeded = self._seed_fsrs_card_from_revlog(
                conn,
                scheduler,
                card_id=int(row["id"]),
                now_dt=review_dt,
            )
            if seeded is not None:
                fsrs_card.stability = seeded.stability
                fsrs_card.difficulty = seeded.difficulty
                fsrs_card.last_review = seeded.last_review
            else:
                # Fallback for imported/legacy cards with no usable revlog.
                ivl_days = int(row["ivl"] or 0)
                factor = int(row["factor"] or 0)

                fsrs_card.stability = float(max(1, ivl_days))

                if factor > 0:
                    ease_mult = max(1.3, min(3.0, factor / 1000.0))
                    scaled = (ease_mult - 1.3) / (3.0 - 1.3)  # 0..1
                    fsrs_card.difficulty = max(1.0, min(10.0, 10.0 - (scaled * 9.0)))
                else:
                    fsrs_card.difficulty = 5.0

                mod_sec = int(row["mod"] or int(review_dt.timestamp()))
                try:
                    fsrs_card.last_review = datetime.fromtimestamp(mod_sec, tz=UTC)
                except (OSError, OverflowError, ValueError):
                    fsrs_card.last_review = review_dt

        if fsrs_card.state == State.Relearning and fsrs_card.step is None:
            fsrs_card.step = 0

        return _FsrsReviewSetup(
            card=fsrs_card,
            scheduler=scheduler,
            timing=timing,
            desired_retention=desired_retention,
            learn_step_count=learn_count,
            relearn_step_count=relearn_count,
            params_source=params_source,
        )

    def _build_scheduler_ex(
        self,
        conn: sqlite3.Connection,
        deck_id: int,
    ) -> tuple[Scheduler, float, int, int, str]:
        """``_build_scheduler`` plus a label for which FSRS weights were used."""
        scheduler, retention, learn_n, relearn_n = self._build_scheduler(conn, deck_id)
        return scheduler, retention, learn_n, relearn_n, self._fsrs_params_source(conn, deck_id)

    def _fsrs_params_source(self, conn: sqlite3.Connection, deck_id: int) -> str:
        """Informational label for the result payload; never fails a review."""
        try:
            cfg, _retention, _has_row = self._deck_config_for_deck(conn, deck_id)
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return "unknown"
            raise
        params, source = self._pick_fsrs_parameters(cfg)
        try:
            Scheduler(parameters=params)
        except ValueError:
            return "default"
        return source

    def _build_scheduler(
        self,
        conn: sqlite3.Connection,
        deck_id: int,
    ) -> tuple[Scheduler, float, int, int]:
        cfg, deck_retention, has_config_row = self._deck_config_for_deck(conn, deck_id)
        params, _params_source = self._pick_fsrs_parameters(cfg)
        desired_retention = (
            deck_retention
            if deck_retention is not None
            else float(cfg.desired_retention or 0.9)
        )
        max_interval = int(cfg.maximum_review_interval or 36500)

        # Repeated proto fields cannot express "unset", so a deck_config row
        # always supplies the step lists -- including empty ones, which mean
        # "no (re)learning steps" exactly like blanked steps in Anki. Only when
        # the row itself is missing do py-fsrs's built-in defaults apply.
        step_kwargs: _SchedulerStepKwargs = (
            {
                "learning_steps": self._to_timedeltas(
                    cfg.learn_steps, assume_minutes=True
                ),
                "relearning_steps": self._to_timedeltas(
                    cfg.relearn_steps, assume_minutes=True
                ),
            }
            if has_config_row
            else {}
        )
        try:
            scheduler = Scheduler(
                parameters=params,
                desired_retention=desired_retention,
                maximum_interval=max_interval,
                **step_kwargs,
            )
        except ValueError:
            # Upgraded legacy weights can land just outside py-fsrs's bounds.
            scheduler = Scheduler(
                parameters=list(FSRS6_DEFAULT_PARAMETERS),
                desired_retention=desired_retention,
                maximum_interval=max_interval,
                **step_kwargs,
            )
        return (
            scheduler,
            desired_retention,
            len(scheduler.learning_steps),
            len(scheduler.relearning_steps),
        )

    def _pick_fsrs_parameters(self, cfg: DeckConfigConfig) -> tuple[list[float], str]:
        """Newest non-empty weight set on the deck config, upgraded to FSRS-6."""
        for candidate in (cfg.fsrs_params_6, cfg.fsrs_params_5, cfg.fsrs_params_4):
            values = [float(item) for item in candidate]
            if values:
                params, source = upgrade_fsrs_parameters(values)
                if source == "default":
                    return params, source
                params, changed = clamp_fsrs_parameters(params)
                return params, f"{source}-clamped" if changed else source
        return list(FSRS6_DEFAULT_PARAMETERS), "default"

    @staticmethod
    def _review_with_fuzz_seed(
        scheduler: Scheduler,
        card: FSRSCard,
        rating: Rating,
        *,
        review_datetime: datetime,
        card_id: int,
        reps: int,
    ) -> FSRSCard:
        """py-fsrs draws its interval fuzz from the module-level ``random``;
        seed it per card + rep count like rslib so preview and answer agree."""
        state = random.getstate()
        try:
            random.seed(fsrs_fuzz_seed(card_id, reps))
            next_card, _log = scheduler.review_card(card, rating, review_datetime=review_datetime)
        finally:
            random.setstate(state)
        return next_card

    @staticmethod
    def _last_review_time(conn: sqlite3.Connection, card_id: int) -> datetime | None:
        """Most recent real review of the card, from the revlog (Anki's source of
        truth); manual reschedules (type 4/5) don't count."""
        row = conn.execute(
            """
            SELECT id FROM revlog
            WHERE cid = ? AND ease IN (1, 2, 3, 4) AND type IN (0, 1, 2, 3)
            ORDER BY id DESC LIMIT 1
            """,
            (card_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            return datetime.fromtimestamp(int(row["id"]) / 1000.0, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None

    def _to_timedeltas(
        self,
        values: list[float],
        *,
        assume_minutes: bool,
    ) -> list[timedelta]:
        """Convert configured step values to timedeltas, keeping empty empty.

        No implicit default: an empty step list is a real configuration (Anki
        lets users blank the steps), not a missing one.
        """
        out: list[timedelta] = []
        for value in values:
            raw = float(value)
            if raw <= 0:
                continue
            seconds = raw * 60.0 if assume_minutes else raw
            out.append(timedelta(seconds=max(1, round(seconds))))
        return out

    def _card_row_to_fsrs(
        self,
        row: sqlite3.Row,
        *,
        timing: SchedTiming,
        now_dt: datetime,
        learn_step_count: int = 0,
        relearn_step_count: int = 0,
    ) -> FSRSCard:
        raw_data = self._parse_card_data(str(row["data"] or ""))
        data: dict[str, JSONValue] = (
            {str(key): cast(JSONValue, value) for key, value in raw_data.items()}
            if isinstance(raw_data, dict)
            else {}
        )

        stability = self._coerce_float_value(data.get("s"))
        difficulty = self._coerce_float_value(data.get("d"))

        # rslib CardData.last_review_time ("lrt"); callers fall back to the
        # revlog when it is absent (cards last answered by an older Anki).
        last_review: datetime | None = None
        lrt_value = self._coerce_int_value(data.get("lrt"))
        if lrt_value is not None:
            try:
                last_review = datetime.fromtimestamp(lrt_value, tz=UTC)
            except (TypeError, ValueError, OSError, OverflowError):
                last_review = None

        card_type = int(row["type"])
        queue = int(row["queue"])
        due_raw = self._scheduling_due(row)
        if card_type == 2:
            due_dt = datetime.fromtimestamp(timing.day_start_epoch(due_raw), tz=UTC)
            state = State.Review
        elif queue in (1, 3) or card_type in (1, 3):
            if is_intraday_learn_due(due_raw):
                due_dt = datetime.fromtimestamp(due_raw, tz=UTC)
            else:
                # Day-learn: due is a scheduling-day index.
                due_dt = datetime.fromtimestamp(timing.day_start_epoch(due_raw), tz=UTC)
            # Relearning is a property of type (3), not of the day-learn queue.
            state = State.Relearning if card_type == 3 else State.Learning
        else:
            due_dt = now_dt
            state = State.Learning

        # Anki packs left = today_remaining * 1000 + remaining_steps; the FSRS
        # step index is how many of the deck's steps are already behind us.
        left_raw = int(row["left"])
        step: int | None
        if left_raw > 0 and state in (State.Learning, State.Relearning):
            remaining = left_raw % 1000
            total = relearn_step_count if state == State.Relearning else learn_step_count
            step = max(0, total - remaining) if total > 0 else 0
        else:
            step = None

        return FSRSCard(
            card_id=int(row["id"]),
            state=state,
            step=step,
            stability=stability,
            difficulty=difficulty,
            due=due_dt,
            last_review=last_review,
        )

    def _map_fsrs_result_to_anki(
        self,
        *,
        current_row: sqlite3.Row,
        next_card: FSRSCard,
        timing: SchedTiming,
        learn_step_count: int,
        relearn_step_count: int,
        now_dt: datetime,
    ) -> tuple[int, int, int, int, int, int]:
        """Translate an FSRS result into Anki's ``(type, queue, due, ivl, left, due_epoch)``.

        ``now_dt`` must be the same instant handed to the FSRS scheduler as
        ``review_datetime`` so interval arithmetic is exact.
        """
        next_due_dt = next_card.due if next_card.due is not None else now_dt
        next_due_epoch = int(next_due_dt.timestamp())
        now_epoch = int(now_dt.timestamp())
        today_days = timing.days_elapsed

        if next_card.state == State.Review:
            # rslib: interval in whole days from today, due = today + interval.
            ivl_days = max(1, round((next_due_dt - now_dt).total_seconds() / 86400.0))
            due_days = today_days + ivl_days
            return (2, 2, due_days, ivl_days, 0, next_due_epoch)

        # Anki keeps learning steps shorter than a day in the intraday queue
        # (epoch due) and moves longer steps to the day-learn queue, whose due
        # is today + round(step / 1 day) (rslib LearnState -> InDays).
        def learn_queue_and_due() -> tuple[int, int]:
            delta = next_due_epoch - now_epoch
            if delta >= 86400:
                return (3, today_days + max(1, round(delta / 86400.0)))
            return (1, next_due_epoch)

        if next_card.state == State.Relearning:
            total = max(1, relearn_step_count)
            step = int(next_card.step or 0)
            remaining = max(1, total - step)
            left = (remaining * 1000) + remaining
            queue, due = learn_queue_and_due()
            return (3, queue, due, 0, left, next_due_epoch)

        # Learning (new or ongoing)
        old_type = int(current_row["type"])
        new_type = 1 if old_type != 2 else old_type
        total = max(1, learn_step_count)
        step = int(next_card.step or 0)
        remaining = max(1, total - step)
        left = (remaining * 1000) + remaining
        queue, due = learn_queue_and_due()
        return (new_type, queue, due, 0, left, next_due_epoch)

    def _seed_fsrs_card_from_revlog(
        self,
        conn: sqlite3.Connection,
        scheduler: Scheduler,
        *,
        card_id: int,
        now_dt: datetime,
    ) -> FSRSCard | None:
        rows = conn.execute(
            """
            SELECT id, ease, time
            FROM revlog
            WHERE cid = ? AND ease IN (1, 2, 3, 4)
            ORDER BY id ASC
            """,
            (card_id,),
        ).fetchall()

        if not rows:
            return None

        logs: list[ReviewLog] = []
        for r in rows:
            rid_ms = int(r["id"])
            ease = int(r["ease"])
            try:
                reviewed_at = datetime.fromtimestamp(rid_ms / 1000.0, tz=UTC)
            except (OSError, OverflowError, ValueError):
                continue

            logs.append(
                ReviewLog(
                    card_id=card_id,
                    rating=Rating(ease),
                    review_datetime=reviewed_at,
                    review_duration=int(r["time"]) if r["time"] is not None else None,
                )
            )

        if not logs:
            return None

        base = FSRSCard(card_id=card_id, due=now_dt)
        seeded = scheduler.reschedule_card(card=base, review_logs=logs)

        if seeded.stability is None or seeded.difficulty is None or seeded.last_review is None:
            return None
        return seeded
