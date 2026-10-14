"""ReviewApp mounted for real under Textual's Pilot.

The layout and focus defects from the Sep 2026 audit (#78 #80 #81 #82 #83) and
the #84 redesign are pinned here by measuring widget regions or driving keys, so
a CSS or compose change that re-introduces one fails here instead of in a
screenshot nobody looks at.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

pytest.importorskip("textual")

from rich.text import Text
from textual.widgets import Button, Footer, HelpPanel, Input, Static

from anki_cli.tui.review_app import ReviewApp
from tests.tui.fake_backend import FakeBackend, make_card

pytestmark = pytest.mark.tui


@pytest.fixture
def fake_backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def run_async() -> Any:
    """Run a coroutine to completion; the suite has no async plugin."""
    return asyncio.run


async def _settle(pilot: Any) -> None:
    """Wait for the card-loading worker, then for the UI to repaint."""
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


def _plain(app: ReviewApp, selector: str) -> str:
    content = app.query_one(selector, Static).content
    return content.plain if isinstance(content, Text) else str(content)


def _label(app: ReviewApp, selector: str) -> str:
    return str(app.query_one(selector, Button).label)


def test_footer_legend_and_help_panel(fake_backend: FakeBackend, run_async: Any) -> None:
    """The hand-built hint bar is replaced by Footer; ``?`` opens the help (#7)."""

    async def scenario() -> None:
        app = ReviewApp(backend=fake_backend, deck=None)
        async with app.run_test(size=(120, 36)) as pilot:
            await _settle(pilot)
            assert app.query_one(Footer).region.height == 1
            assert not app.query("#hintbar") and not app.query("#status")
            assert not app.query(HelpPanel)
            await pilot.press("question_mark")
            await pilot.pause()
            assert app.query(HelpPanel)

    run_async(scenario())


def test_failures_are_toasts(fake_backend: FakeBackend, run_async: Any) -> None:
    async def scenario() -> None:
        app = ReviewApp(backend=fake_backend, deck=None)
        async with app.run_test(size=(120, 36)) as pilot:
            await _settle(pilot)
            app._set_status("answer failed: collection locked")
            app._set_status("undone")
            await pilot.pause()
            notes = {n.message: n.severity for n in app._notifications}
            assert notes["answer failed: collection locked"] == "error"
            assert notes["undone"] == "information"

    run_async(scenario())


def test_action_row_morphs_when_the_answer_is_revealed(
    fake_backend: FakeBackend, run_async: Any
) -> None:
    """The answer block and the four ratings only exist once the answer is up."""

    async def scenario() -> None:
        app = ReviewApp(backend=fake_backend, deck=None)
        async with app.run_test(size=(120, 36)) as pilot:
            await _settle(pilot)
            assert app.query_one("#show-answer").display
            assert not app.query_one("#rate-row").display
            assert not app.query_one("#answer-panel").display
            assert "capital of France" in _plain(app, "#question")

            await pilot.press("space")
            await pilot.pause()
            assert not app.query_one("#show-answer").display
            assert app.query_one("#rate-row").display
            assert app.query_one("#answer-panel").display
            assert "Paris" in _plain(app, "#answer")

            # Real Buttons, one variant per rating, with the scheduler's interval;
            # the "no due_info" item falls back to the raw interval, never "?".
            variants = [app.query_one(f"#rate-{n}", Button).variant for n in (1, 2, 3, 4)]
            assert variants == ["error", "warning", "success", "primary"]
            assert _label(app, "#rate-2") == "2 Hard · 36d"
            assert "?" not in _label(app, "#rate-1")

            await pilot.press("space")
            await pilot.pause()
            assert app.query_one("#show-answer").display
            assert not app.query_one("#rate-row").display

    run_async(scenario())


def test_clicking_buttons_flips_and_rates(fake_backend: FakeBackend, run_async: Any) -> None:
    async def scenario() -> None:
        app = ReviewApp(backend=fake_backend, deck=None)
        async with app.run_test(size=(120, 36)) as pilot:
            await _settle(pilot)
            await pilot.click("#show-answer")
            await pilot.pause()
            assert app._show_answer is True
            await pilot.click("#rate-3")
            await _settle(pilot)
            assert ("answer_card", (1000, 3)) in fake_backend.calls

    run_async(scenario())


@pytest.mark.parametrize("size", [(130, 38), (100, 30), (80, 24)])
def test_layout_fits_at_every_size(
    fake_backend: FakeBackend, run_async: Any, size: tuple[int, int]
) -> None:
    """The action row sits above the footer, the card is a centred column of at
    most 90 cells, and ``Screen.-short`` drops the buttons to one row."""

    async def scenario() -> None:
        app = ReviewApp(backend=fake_backend, deck=None)
        async with app.run_test(size=size) as pilot:
            await _settle(pilot)
            await pilot.press("space")
            await pilot.pause()
            width, height = size
            footer = app.query_one(Footer).region
            rate = app.query_one("#rate-row").region
            assert rate.y + rate.height <= footer.y, (size, rate, footer)
            assert rate.height == (1 if height < 30 else 3), (size, rate)
            for n in (1, 2, 3, 4):
                btn = app.query_one(f"#rate-{n}").region
                assert btn.height == rate.height and btn.width >= 8, (size, n, btn)
                assert btn.right <= width, (size, n, btn)
            for sel in ("#question-panel", "#answer-panel"):
                assert app.query_one(sel).region.height >= 3, (size, sel)
            column = app.query_one("#column").region
            assert column.width <= 90, (size, column)
            if width > 94:
                # Centred: equal gutters either side.
                assert abs(column.x - (width - column.right)) <= 1, (size, column)

    run_async(scenario())


def test_keys_work_at_startup_without_pressing_escape(
    fake_backend: FakeBackend, run_async: Any
) -> None:
    """#81: the caret must not start in a text box, so Space and 1-4 act."""

    async def scenario() -> None:
        app = ReviewApp(backend=fake_backend, deck=None)
        async with app.run_test(size=(120, 36)) as pilot:
            await _settle(pilot)
            assert app.focused is None
            assert app._show_answer is False
            await pilot.press("space")
            assert app._show_answer is True
            await pilot.press("3")
            await _settle(pilot)
            assert ("answer_card", (1000, 3)) in fake_backend.calls

    run_async(scenario())


def test_command_input_exists_only_while_invoked(fake_backend: FakeBackend, run_async: Any) -> None:
    async def scenario() -> None:
        app = ReviewApp(backend=fake_backend, deck=None)
        async with app.run_test(size=(120, 36)) as pilot:
            await _settle(pilot)
            assert not app.query("#cmd")
            await pilot.press("colon")
            await pilot.pause()
            assert isinstance(app.focused, Input)
            assert app.focused.value == ":"
            await pilot.press("escape")
            await pilot.pause()
            assert not app.query("#cmd")
            assert app.focused is None

            # A submitted command runs and the input goes away again.
            await pilot.press("colon", "s", "h", "o", "w", "enter")
            await pilot.pause()
            assert not app.query("#cmd")
            assert app._show_answer is True

    run_async(scenario())


def test_card_text_with_brackets_renders_verbatim(run_async: Any) -> None:
    """#82: ``Static.update(str)`` parsed markup, so ``[x]`` / ``[/]`` vanished."""
    front = "arr[i] = x[0] and [/] tag; [x] done; 5 [mg] dose"
    backend = FakeBackend(cards=[make_card(0, front=front)])

    async def scenario() -> None:
        app = ReviewApp(backend=backend, deck=None)
        async with app.run_test(size=(120, 36)) as pilot:
            await _settle(pilot)
            assert _plain(app, "#question") == front
            await pilot.press("space")
            await pilot.pause()
            assert front in _plain(app, "#answer")

    run_async(scenario())


def test_snapshot_failure_is_reported_not_raised(fake_backend: FakeBackend, run_async: Any) -> None:
    """#83: ``snapshot_card_state`` sat outside the try, so a locked
    collection produced a Textual traceback and ``return_code == 1``."""
    fake_backend.snapshot_error = RuntimeError("collection is locked")

    async def scenario() -> None:
        app = ReviewApp(backend=fake_backend, deck=None)
        async with app.run_test(size=(120, 36)) as pilot:
            await _settle(pilot)
            await pilot.press("space", "3")
            await pilot.pause()
            messages = [n.message for n in app._notifications]
            assert any("collection is locked" in m for m in messages), messages
            assert not any(name == "answer_card" for name, _ in fake_backend.calls)
        # run_test exits without a code; a crash would have set 1 (and re-raised).
        assert not app.return_code

    run_async(scenario())


class _Counting:
    """Delegates to the fake, counting calls and the threads they ran on."""

    def __init__(self, inner: FakeBackend) -> None:
        self._inner = inner
        self.counts: dict[str, int] = {}
        self.threads: dict[str, set[int]] = {}

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self.counts[name] = self.counts.get(name, 0) + 1
            self.threads.setdefault(name, set()).add(threading.get_ident())
            return attr(*args, **kwargs)

        return wrapper


def test_flipping_the_answer_does_not_touch_the_backend(
    fake_backend: FakeBackend, run_async: Any
) -> None:
    """#35: every keypress used to re-fetch the card, note, notetype, rating
    previews and due counts. A card is rendered once; flipping repaints."""
    backend = _Counting(fake_backend)

    async def scenario() -> None:
        app = ReviewApp(backend=backend, deck=None)
        async with app.run_test(size=(120, 36)) as pilot:
            await _settle(pilot)
            before = dict(backend.counts)
            assert not app.query_one("#answer-panel").display

            await pilot.press("space")
            await pilot.pause()
            assert "Paris" in _plain(app, "#answer")
            await pilot.press("space")
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()

            assert backend.counts == before

    run_async(scenario())


def test_next_card_is_loaded_off_the_ui_thread(fake_backend: FakeBackend, run_async: Any) -> None:
    """Picking the next card, rendering it, the rating previews and the due
    counts all ran on the UI thread on every answer (#84, follow-up to #35)."""
    backend = _Counting(fake_backend)
    ui_thread = threading.get_ident()

    async def scenario() -> None:
        app = ReviewApp(backend=backend, deck=None)
        async with app.run_test(size=(120, 36)) as pilot:
            await _settle(pilot)
            await pilot.press("space", "3")
            await _settle(pilot)
            for name in ("get_next_due_card", "get_card", "preview_ratings", "get_due_counts"):
                assert backend.threads[name], name
                assert ui_thread not in backend.threads[name], name

    run_async(scenario())


def test_keys_are_ignored_while_the_next_card_loads(
    fake_backend: FakeBackend, run_async: Any
) -> None:
    """A second rating while the next card loads must not answer the old card."""
    gate = threading.Event()
    picks = {"n": 0}
    original = fake_backend.get_next_due_card

    def slow_pick(deck: str | None = None) -> Any:
        picks["n"] += 1
        if picks["n"] > 1:
            gate.wait(5)
        return original(deck)

    fake_backend.get_next_due_card = slow_pick  # type: ignore[method-assign]

    async def scenario() -> None:
        app = ReviewApp(backend=fake_backend, deck=None)
        async with app.run_test(size=(120, 36)) as pilot:
            await _settle(pilot)
            await pilot.press("space", "3")
            assert app._loading is True
            await pilot.press("3", "3")
            gate.set()
            await _settle(pilot)
            answered = [c for c in fake_backend.calls if c[0] == "answer_card"]
            assert len(answered) == 1, answered

    run_async(scenario())
