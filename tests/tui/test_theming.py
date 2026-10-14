"""Both apps follow the active Textual theme instead of hard-coded hex (#85, #5)."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

pytest.importorskip("textual")

from rich.text import Text
from textual.widgets import DataTable, Static

import anki_cli.tui.browse_app as browse_mod
import anki_cli.tui.review_app as review_mod
from anki_cli.models.config import DEFAULT_THEME, AppConfig
from anki_cli.tui.colors import TOKYO_NIGHT
from anki_cli.tui.theming import DEFAULT_PALETTE, NO_COLOR_THEME, Palette
from tests.tui.fake_backend import FakeBackend

pytestmark = pytest.mark.tui

_HEX = re.compile(r"(?<![\w-])#[0-9a-fA-F]{6}\b")


@pytest.fixture
def run_async() -> Any:
    return asyncio.run


def test_no_hard_coded_hex_in_any_stylesheet() -> None:
    """Every colour in the CSS is a ``$variable``, so a theme change reaches it."""
    sheets = {
        "BrowseApp.CSS": browse_mod.BrowseApp.CSS,
        "ReviewApp.CSS": review_mod.ReviewApp.CSS,
        "DetailScreen": browse_mod.DetailScreen.DEFAULT_CSS,
        "PreviewScreen": review_mod.PreviewScreen.DEFAULT_CSS,
    }
    for name, css in sheets.items():
        assert not _HEX.findall(css), (name, _HEX.findall(css))


def test_default_theme_name_matches_the_registered_theme() -> None:
    assert TOKYO_NIGHT.name == DEFAULT_THEME
    assert AppConfig().display.theme == TOKYO_NIGHT.name


def test_palette_follows_theme_variables() -> None:
    tokyo = Palette.from_theme(TOKYO_NIGHT)
    assert tokyo == DEFAULT_PALETTE
    assert tokyo.primary.lower() == "#7aa2f7"
    custom = Palette.from_variables({"primary": "#ff0000", "foreground": "#ffffff"})
    assert custom.primary.lower() == "#ff0000"
    # A missing variable falls back to the Tokyo Night value rather than raising.
    assert custom.error == tokyo.error


def test_default_look_is_flat_tokyo_night(run_async: Any) -> None:
    async def scenario() -> None:
        app = browse_mod.BrowseApp(backend=FakeBackend())
        async with app.run_test(size=(130, 30)) as pilot:
            await pilot.pause()
            assert app.theme == TOKYO_NIGHT.name
            assert app.query_one("#preview").styles.background.hex.lower() == "#16161e"
            # Header, footer, panes and table rows share one flat background.
            for sel in ("#header", "#table", "Footer"):
                assert app.query_one(sel).styles.background.hex.lower() == "#16161e", sel

    run_async(scenario())


def test_switching_theme_recolours_the_browse_screen(run_async: Any) -> None:
    async def scenario() -> None:
        app = browse_mod.BrowseApp(backend=FakeBackend())
        async with app.run_test(size=(130, 30)) as pilot:
            await pilot.pause()
            before = app.query_one("#preview").styles.background.hex
            before_cell = app.query_one("#table", DataTable).get_row_at(0)[1]
            app.theme = "nord"
            await pilot.pause()
            await pilot.pause()
            assert app.query_one("#preview").styles.background.hex != before
            # Rich text built in Python is rebuilt too, not left on the old colours.
            after_cell = app.query_one("#table", DataTable).get_row_at(0)[1]
            assert isinstance(after_cell, Text)
            assert after_cell.style != before_cell.style
            assert after_cell.style == app.palette.accent

    run_async(scenario())


def test_switching_theme_recolours_the_review_screen(run_async: Any) -> None:
    async def scenario() -> None:
        app = review_mod.ReviewApp(backend=FakeBackend(), deck=None)
        async with app.run_test(size=(120, 36)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            content = app.query_one("#header-stats", Static).content
            before = {str(span.style) for span in content.spans}
            app.theme = "dracula"
            await pilot.pause()
            await pilot.pause()
            content = app.query_one("#header-stats", Static).content
            after = {str(span.style) for span in content.spans}
            assert before != after
            assert f"bold {app.palette.primary}" in after

    run_async(scenario())


def test_configured_theme_is_applied_and_unknown_theme_warns(run_async: Any) -> None:
    async def scenario() -> None:
        app = browse_mod.BrowseApp(backend=FakeBackend(), theme="nord")
        async with app.run_test(size=(130, 30)) as pilot:
            await pilot.pause()
            assert app.theme == "nord"

        bad = browse_mod.BrowseApp(backend=FakeBackend(), theme="no-such-theme")
        async with bad.run_test(size=(130, 30)) as pilot:
            await pilot.pause()
            assert bad.theme == TOKYO_NIGHT.name
            assert any("no-such-theme" in n.message for n in bad._notifications)

    run_async(scenario())


def test_no_color_uses_the_terminals_own_palette(run_async: Any) -> None:
    async def scenario() -> None:
        for app in (
            browse_mod.BrowseApp(backend=FakeBackend(), no_color=True),
            review_mod.ReviewApp(backend=FakeBackend(), deck=None, no_color=True),
        ):
            async with app.run_test(size=(120, 36)) as pilot:
                await pilot.pause()
                assert app.theme == NO_COLOR_THEME
                assert app.ansi_color is True
                # Rich styles resolve to ANSI colour names, never a truecolour hex.
                assert not _HEX.match(app.palette.primary), app.palette

    run_async(scenario())


def test_picked_theme_is_saved_unless_no_color(run_async: Any) -> None:
    saved: list[str] = []

    async def scenario() -> None:
        app = browse_mod.BrowseApp(backend=FakeBackend(), persist_theme=saved.append)
        async with app.run_test(size=(130, 30)) as pilot:
            await pilot.pause()
            app.theme = "nord"
            await pilot.pause()
            await pilot.pause()
        assert saved == ["nord"]

        # --no-color is a per-run choice and must not overwrite the saved theme.
        quiet: list[str] = []
        plain = browse_mod.BrowseApp(
            backend=FakeBackend(), no_color=True, persist_theme=quiet.append
        )
        async with plain.run_test(size=(130, 30)) as pilot:
            await pilot.pause()
            plain.theme = "nord"
            await pilot.pause()
            await pilot.pause()
        assert quiet == []

    run_async(scenario())


def test_failing_save_is_a_toast_not_a_crash(run_async: Any) -> None:
    def boom(name: str) -> None:
        raise OSError("read-only config")

    async def scenario() -> None:
        app = browse_mod.BrowseApp(backend=FakeBackend(), persist_theme=boom)
        async with app.run_test(size=(130, 30)) as pilot:
            await pilot.pause()
            app.theme = "nord"
            await pilot.pause()
            await pilot.pause()
            assert any("read-only config" in n.message for n in app._notifications)
        assert not app.return_code

    run_async(scenario())
