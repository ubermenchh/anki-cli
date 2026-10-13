"""ReviewApp mounted for real under Textual's Pilot.

Each test pins one of the layout/focus defects from the Sep 2026 audit
(#78 #80 #81 #82 #83) by measuring widget regions or driving keys, so a CSS or
compose change that re-introduces the defect fails here instead of in a
screenshot nobody looks at.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("textual")

from rich.text import Text
from textual.widgets import Input, Static

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


def _plain(app: ReviewApp, selector: str) -> str:
    content = app.query_one(selector, Static).content
    return content.plain if isinstance(content, Text) else str(content)


def test_bottom_bars_do_not_overlap(fake_backend: FakeBackend, run_async: Any) -> None:
    """#78: hintbar, status and cmd were all ``dock: bottom`` and drew on the
    same row, so the key hints and every error message were invisible."""

    async def scenario() -> None:
        app = ReviewApp(backend=fake_backend, deck=None)
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            rows = {sel: app.query_one(sel).region for sel in ("#status", "#hintbar", "#cmd")}
            ys = [r.y for r in rows.values()]
            assert len(set(ys)) == 3, rows
            assert all(r.height >= 1 for r in rows.values()), rows
            # And the hint bar has a content row to draw into (#80-style clipping).
            assert app.query_one("#hintbar").content_region.height >= 1

    run_async(scenario())


def test_status_message_is_visible_on_screen(fake_backend: FakeBackend, run_async: Any) -> None:
    async def scenario() -> None:
        app = ReviewApp(backend=fake_backend, deck=None)
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            app._set_status("answer failed: collection locked")
            await pilot.pause()
            status = app.query_one("#status", Static)
            assert status.region.height == 1
            assert "collection locked" in _plain(app, "#status")

    run_async(scenario())


def test_side_titles_and_rate_hints_have_room(fake_backend: FakeBackend, run_async: Any) -> None:
    """#80: ``height: 1`` + a border left zero content rows for the SESSION /
    TODAY / REMAINING labels, and ``height: 3`` clipped the interval hint off
    the rating buttons."""

    async def scenario() -> None:
        app = ReviewApp(backend=fake_backend, deck=None)
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            for title in app.query(".side-title"):
                assert title.content_region.height >= 1, title
            await pilot.press("space")
            await pilot.pause()
            for n in (1, 2, 3, 4):
                btn = app.query_one(f"#rate-{n}")
                assert btn.content_region.height >= 2, (n, btn.content_region)
            # The hint line is populated, and the "no due_info" item falls back
            # to the raw interval instead of "?".
            assert _plain(app, "#rate-2").splitlines()[1] == "36d"
            assert "?" not in _plain(app, "#rate-1")

    run_async(scenario())


@pytest.mark.parametrize("size", [(130, 38), (100, 30), (80, 24)])
def test_rate_row_stays_on_screen(
    fake_backend: FakeBackend, run_async: Any, size: tuple[int, int]
) -> None:
    """The 4-row rate row must not be pushed below the bottom bars, and both
    of its text lines must have room, at every supported terminal size."""

    async def scenario() -> None:
        app = ReviewApp(backend=fake_backend, deck=None)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()
            rate = app.query_one("#rate-row").region
            bottom = app.query_one("#bottom").region
            assert rate.height == 4, (size, rate)
            assert rate.y + rate.height <= bottom.y, (size, rate, bottom)
            assert app.query_one("#rate-1").content_region.height >= 2
            # Question and answer panels are both still visible.
            for sel in ("#question-panel", "#answer-panel"):
                assert app.query_one(sel).region.height >= 4, (size, sel)

    run_async(scenario())


def test_keys_work_at_startup_without_pressing_escape(
    fake_backend: FakeBackend, run_async: Any
) -> None:
    """#81: AUTO_FOCUS put the caret in #cmd, so Space and 1-4 typed text."""

    async def scenario() -> None:
        app = ReviewApp(backend=fake_backend, deck=None)
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            assert app.focused is None
            assert app._show_answer is False
            await pilot.press("space")
            assert app._show_answer is True
            await pilot.press("3")
            await pilot.pause()
            assert ("answer_card", (1000, 3)) in fake_backend.calls
            assert app.query_one("#cmd", Input).value == ""
            # ":" still enters command mode; Esc leaves it.
            await pilot.press("colon")
            assert isinstance(app.focused, Input)
            await pilot.press("escape")
            assert app.focused is None

    run_async(scenario())


def test_card_text_with_brackets_renders_verbatim(run_async: Any) -> None:
    """#82: ``Static.update(str)`` parsed markup, so ``[x]`` / ``[/]`` vanished."""
    front = "arr[i] = x[0] and [/] tag; [x] done; 5 [mg] dose"
    backend = FakeBackend(cards=[make_card(0, front=front)])

    async def scenario() -> None:
        app = ReviewApp(backend=backend, deck=None)
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
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
            await pilot.pause()
            await pilot.press("space", "3")
            await pilot.pause()
            assert "collection is locked" in _plain(app, "#status")
            assert not any(name == "answer_card" for name, _ in fake_backend.calls)
        # run_test exits without a code; a crash would have set 1 (and re-raised).
        assert not app.return_code

    run_async(scenario())
