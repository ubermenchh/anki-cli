"""BrowseApp mounted for real under Textual's Pilot (#78 #79 #81 #82)."""

from __future__ import annotations

import asyncio
import itertools
from typing import Any

import pytest

pytest.importorskip("textual")

from rich.text import Text
from textual.widgets import DataTable, Input, Static

from anki_cli.tui.browse_app import BrowseApp
from tests.tui.fake_backend import FakeBackend, make_card

pytestmark = pytest.mark.tui


@pytest.fixture
def fake_backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def run_async() -> Any:
    """Run a coroutine to completion; the suite has no async plugin."""
    return asyncio.run


async def _mounted(pilot: Any, app: BrowseApp) -> DataTable:
    """Wait for the load worker and its call_from_thread callback to land."""
    for _ in range(20):
        await pilot.pause(0.05)
        table = app.query_one("#table", DataTable)
        if table.row_count:
            return table
    raise AssertionError("table never populated")


def _header_labels(table: DataTable) -> list[str]:
    return [str(col.label) for col in table.columns.values()]


def test_top_bars_do_not_overlap(fake_backend: FakeBackend, run_async: Any) -> None:
    """#78: titlebar and toolbar were both ``dock: top`` at y=0."""

    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="deck:x")
        async with app.run_test(size=(130, 38)) as pilot:
            await pilot.pause()
            rows = [app.query_one(sel).region for sel in ("#header", "#search-wrap", "#filters")]
            for above, below in itertools.pairwise(rows):
                assert above.y + above.height <= below.y, rows
            assert app.query_one("#table").region.y >= rows[-1].y + rows[-1].height

    run_async(scenario())


@pytest.mark.parametrize("size", [(160, 40), (130, 38), (110, 30), (100, 30), (80, 24)])
def test_all_columns_fit_without_horizontal_scroll(
    fake_backend: FakeBackend, run_async: Any, size: tuple[int, int]
) -> None:
    """#79: Due and Ivl sat past the right edge at every terminal width."""

    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="deck:x")
        async with app.run_test(size=size) as pilot:
            table = await _mounted(pilot, app)
            await pilot.pause()
            assert _header_labels(table) == ["Question", "Deck", "Due", "Ivl", "Type"]
            assert table.virtual_size.width <= table.region.width, (
                size,
                table.virtual_size,
                table.region,
            )
            preview = app.query_one("#preview")
            assert preview.display
            if size[0] >= 100:
                # Wide: beside the table.
                assert 30 <= preview.region.width <= 60
                assert preview.region.x >= table.region.x + table.region.width
            else:
                # Narrow: a strip under the table, full width.
                assert preview.region.y >= table.region.y + table.region.height
                assert preview.region.width >= table.region.width

    run_async(scenario())


def test_resize_refits_question_column(fake_backend: FakeBackend, run_async: Any) -> None:
    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="deck:x")
        async with app.run_test(size=(130, 38)) as pilot:
            table = await _mounted(pilot, app)
            wide = app._current_question_width
            table.move_cursor(row=3, column=0)
            await pilot.resize_terminal(90, 30)
            await pilot.pause(0.2)
            table = app.query_one("#table", DataTable)
            assert app._current_question_width != wide
            assert table.virtual_size.width <= table.region.width
            assert table.row_count == len(fake_backend.cards)
            # The rebuild keeps the user's place.
            assert table.cursor_row == 3

    run_async(scenario())


def test_table_has_focus_and_keys_act_at_startup(fake_backend: FakeBackend, run_async: Any) -> None:
    """#81: focus landed in the search Input, so ``s`` typed an "s"."""

    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="deck:x")
        async with app.run_test(size=(130, 38)) as pilot:
            await _mounted(pilot, app)
            assert isinstance(app.focused, DataTable)
            await pilot.press("s")
            await pilot.pause()
            assert ("suspend_cards", ((1000,),)) in fake_backend.calls
            assert app.query_one("#search", Input).value == "deck:x"

    run_async(scenario())


def test_escape_from_search_returns_to_table_instead_of_quitting(
    fake_backend: FakeBackend, run_async: Any
) -> None:
    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="deck:x")
        async with app.run_test(size=(130, 38)) as pilot:
            await _mounted(pilot, app)
            await pilot.press("slash")
            assert isinstance(app.focused, Input)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.focused, DataTable)
            assert app.is_running

    run_async(scenario())


def test_header_shows_raw_query_not_markup(fake_backend: FakeBackend, run_async: Any) -> None:
    """#82: the query was echoed through markup parsing."""

    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="tag:[x] deck:[b]ig")
        async with app.run_test(size=(130, 38)) as pilot:
            await _mounted(pilot, app)
            await pilot.pause()
            content = app.query_one("#header-text", Static).content
            assert isinstance(content, Text)
            assert content.plain.endswith("tag:[x] deck:[b]ig")

    run_async(scenario())


# --- #35: batched loads, event-driven preview, writes off the UI thread -------------


def _calls(backend: FakeBackend, name: str) -> list[tuple[Any, ...]]:
    return [args for n, args in backend.calls if n == name]


def test_load_fetches_cards_in_batches_not_one_by_one(
    fake_backend: FakeBackend, run_async: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from anki_cli.tui import browse_app

    monkeypatch.setattr(browse_app, "_LOAD_BATCH", 4)
    fetched: list[int] = []
    real = fake_backend.get_card

    def spy(card_id: int) -> dict[str, Any]:
        fetched.append(card_id)
        return real(card_id)

    fake_backend.get_card = spy  # type: ignore[method-assign]

    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            table = await _mounted(pilot, app)
            assert table.row_count == 6

    run_async(scenario())
    assert fetched == []
    assert [len(args[0]) for args in _calls(fake_backend, "get_cards")] == [4, 2]


def test_preview_follows_the_cursor_without_polling(
    fake_backend: FakeBackend, run_async: Any
) -> None:
    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            await _mounted(pilot, app)
            assert not app._timers  # no set_interval poll left
            preview = app.query_one("#preview")
            assert "1000" in str(preview.border_title)
            await pilot.press("down")
            await pilot.pause(0.05)
            assert "1001" in str(preview.border_title)

    run_async(scenario())


def test_suspend_runs_off_the_ui_thread_and_reloads(
    fake_backend: FakeBackend, run_async: Any
) -> None:
    import threading

    seen: list[bool] = []
    real = fake_backend.suspend_cards

    def spy(ids: list[int]) -> dict[str, Any]:
        seen.append(threading.current_thread() is threading.main_thread())
        return real(ids)

    fake_backend.suspend_cards = spy  # type: ignore[method-assign]

    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            await _mounted(pilot, app)
            await pilot.press("s")
            for _ in range(20):
                await pilot.pause(0.05)
                if len(_calls(fake_backend, "find_cards")) >= 2:
                    break
            assert _calls(fake_backend, "suspend_cards") == [((1000,),)]
            assert len(_calls(fake_backend, "find_cards")) >= 2

    run_async(scenario())
    assert seen == [False]


def test_delete_asks_for_confirmation_then_runs_in_a_worker(
    fake_backend: FakeBackend, run_async: Any
) -> None:
    from anki_cli.tui.browse_app import ConfirmScreen

    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            table = await _mounted(pilot, app)
            await pilot.press("d")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            # The question says what goes with the note.
            shown = " ".join(strip.text for strip in app.screen._compositor.render_strips())
            assert "Delete note 5000 and 1 card?" in shown
            assert _calls(fake_backend, "delete_notes") == []

            await pilot.press("y")
            for _ in range(20):
                await pilot.pause(0.05)
                if _calls(fake_backend, "delete_notes"):
                    break
            assert _calls(fake_backend, "delete_notes") == [((5000,),)]
            for _ in range(20):
                await pilot.pause(0.05)
                if table.row_count == 5:
                    break
            assert table.row_count == 5

    run_async(scenario())


def test_delete_confirmation_defaults_to_cancel(fake_backend: FakeBackend, run_async: Any) -> None:
    """Enter, n and Esc all back out; nothing is deleted."""
    from anki_cli.tui.browse_app import ConfirmScreen

    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            await _mounted(pilot, app)
            for key in ("enter", "n", "escape"):
                await pilot.press("d")
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert isinstance(app.screen, ConfirmScreen)
                await pilot.press(key)
                await pilot.pause()
                assert not isinstance(app.screen, ConfirmScreen), key
            assert _calls(fake_backend, "delete_notes") == []

    run_async(scenario())


def test_b_buries_and_unburies_the_highlighted_card(run_async: Any) -> None:
    buried = make_card(0)
    buried["queue"] = -2
    backend = FakeBackend(cards=[make_card(0), buried_card_two(buried)])

    async def scenario() -> None:
        app = BrowseApp(backend=backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            await _mounted(pilot, app)
            await pilot.press("b")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert _calls(backend, "bury_cards") == [((1000,),)]
            assert any("buried" in n.message for n in app._notifications)

            await pilot.press("down", "b")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert _calls(backend, "unbury_cards") == [((1001,),)]

    run_async(scenario())


def buried_card_two(card: dict[str, Any]) -> dict[str, Any]:
    two = dict(card)
    two["cardId"], two["note"], two["queue"] = 1001, 5001, -2
    return two


def test_f_opens_the_flag_picker_and_sets_the_flag(
    fake_backend: FakeBackend, run_async: Any
) -> None:
    from anki_cli.tui.browse_app import FlagScreen

    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            table = await _mounted(pilot, app)
            await pilot.press("f")
            await pilot.pause()
            assert isinstance(app.screen, FlagScreen)

            # Esc leaves the card alone.
            await pilot.press("escape")
            await pilot.pause()
            assert _calls(fake_backend, "set_card_flag") == []

            await pilot.press("f", "3")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert _calls(fake_backend, "set_card_flag") == [((1000,), 3)]
            # The reloaded row now carries the flag marker.
            first = table.get_row_at(0)[0]
            assert isinstance(first, Text) and first.plain.startswith("\u2691")

    run_async(scenario())


def test_row_actions_are_unavailable_without_a_selected_card(run_async: Any) -> None:
    backend = FakeBackend(cards=[])

    async def scenario() -> None:
        app = BrowseApp(backend=backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            await pilot.pause()
            await pilot.pause()
            for action in ("suspend_selected", "bury_selected", "flag_selected", "delete_selected"):
                assert app.check_action(action, ()) is False, action
            await pilot.press("s", "b", "f", "d")
            await pilot.pause()
            for name in ("suspend_cards", "bury_cards", "set_card_flag", "delete_notes"):
                assert _calls(backend, name) == [], name

    run_async(scenario())


def test_preview_shows_the_full_deck_path(fake_backend: FakeBackend, run_async: Any) -> None:
    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            await _mounted(pilot, app)
            meta = app.query_one("#preview-meta", Static).content
            assert "Languages::Japanese::Core 2k" in meta.plain

    run_async(scenario())


def test_z_zooms_the_focused_pane(fake_backend: FakeBackend, run_async: Any) -> None:
    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            await _mounted(pilot, app)
            await pilot.press("z")
            await pilot.pause()
            assert app.screen.maximized is not None
            await pilot.press("z")
            await pilot.pause()
            assert app.screen.maximized is None

    run_async(scenario())


# --- #84: Footer + help, Tabs, panes, toasts -----------------------------------------


def test_footer_shows_the_key_legend_and_question_mark_opens_help(
    fake_backend: FakeBackend, run_async: Any
) -> None:
    from textual.widgets import Footer, HelpPanel

    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            await _mounted(pilot, app)
            footer = app.query_one(Footer)
            assert footer.region.height >= 1
            assert footer.region.y + footer.region.height == app.size.height
            assert not app.query(HelpPanel)
            await pilot.press("question_mark")
            await pilot.pause()
            assert app.query(HelpPanel)

    run_async(scenario())


def test_filter_tabs_filter_the_table_from_keys_and_tab_clicks(
    fake_backend: FakeBackend, run_async: Any
) -> None:
    from textual.widgets import Tabs

    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            table = await _mounted(pilot, app)
            tabs = app.query_one("#filters", Tabs)
            assert tabs.active == "ftab-all"
            assert table.row_count == 6

            await pilot.press("right_square_bracket")  # All -> New (queue 0)
            await pilot.pause()
            assert tabs.active == "ftab-new"
            assert table.row_count == sum(1 for c in fake_backend.cards if c["queue"] == 0)

            await pilot.press("left_square_bracket")
            await pilot.pause()
            assert tabs.active == "ftab-all"
            assert table.row_count == 6

            tabs.active = "ftab-suspended"  # what a click does
            await pilot.pause()
            assert table.row_count == sum(1 for c in fake_backend.cards if c["queue"] == -1)

    run_async(scenario())


def test_digits_focus_panes_and_p_toggles_the_preview(
    fake_backend: FakeBackend, run_async: Any
) -> None:
    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            await _mounted(pilot, app)
            await pilot.press("2")
            assert app.focused is not None and app.focused.id == "preview-scroll"
            await pilot.press("1")
            assert isinstance(app.focused, DataTable)

            preview = app.query_one("#preview")
            await pilot.press("p")
            await pilot.pause()
            assert not preview.display
            await pilot.press("p")
            await pilot.pause()
            assert preview.display

    run_async(scenario())


def test_action_results_are_toasts_not_a_status_line(
    fake_backend: FakeBackend, run_async: Any
) -> None:
    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            await _mounted(pilot, app)
            assert not app.query("#statusbar")
            await pilot.press("s")
            await app.workers.wait_for_complete()
            await pilot.pause()
            messages = [n.message for n in app._notifications]
            assert any("Card 1000 suspended" in m for m in messages), messages

    run_async(scenario())


def test_enter_opens_the_detail_modal_and_escape_closes_it(
    fake_backend: FakeBackend, run_async: Any
) -> None:
    from anki_cli.tui.browse_app import DetailScreen

    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            await _mounted(pilot, app)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, DetailScreen)
            panel = app.screen.query_one("#detail-panel")
            # Titled on the border, sized to its content, and readable.
            assert panel.border_title == "Card 1000"
            assert panel.border_subtitle == "esc to close"
            assert panel.region.height < 38 - 4
            shown = " ".join(strip.text for strip in app.screen._compositor.render_strips())
            assert "Front" in shown and "The capital of France" in shown
            assert "{'kind'" not in shown
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, DetailScreen)

    run_async(scenario())


# --- #86: multi-select and typed actions -------------------------------------------


def _screen_text(app: Any) -> str:
    return " ".join(strip.text for strip in app.screen._compositor.render_strips())


async def _settle(pilot: Any, app: BrowseApp) -> None:
    await app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


def test_space_ticks_the_card_and_moves_on(fake_backend: FakeBackend, run_async: Any) -> None:
    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            table = await _mounted(pilot, app)
            await pilot.press("space")
            await pilot.pause()
            assert app._marked == {1000}
            assert table.cursor_row == 1
            first = table.get_row_at(0)[0]
            assert isinstance(first, Text) and first.plain.startswith("✓")
            assert "1 selected" in str(app.query_one("#header-count", Static).content)

            # Ticking it again (back on row 0) clears the mark.
            await pilot.press("up", "space")
            await pilot.pause()
            assert app._marked == set()
            assert "selected" not in str(app.query_one("#header-count", Static).content)

    run_async(scenario())


def test_batch_actions_apply_to_every_ticked_card(
    fake_backend: FakeBackend, run_async: Any
) -> None:
    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            await _mounted(pilot, app)
            await pilot.press("space", "space", "space")  # cards 1000, 1001, 1002
            await pilot.press("s")
            await _settle(pilot, app)
            suspended = _calls(fake_backend, "suspend_cards")
            assert suspended == [((1000, 1001, 1002),)]
            assert any("3 cards suspended" in n.message for n in app._notifications)

            await pilot.press("f", "3")
            await _settle(pilot, app)
            assert _calls(fake_backend, "set_card_flag") == [((1000, 1001, 1002), 3)]
            # Ticks survive the reload that follows an action.
            assert app._marked == {1000, 1001, 1002}

    run_async(scenario())


def test_without_ticks_actions_use_the_highlighted_card(
    fake_backend: FakeBackend, run_async: Any
) -> None:
    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            await _mounted(pilot, app)
            await pilot.press("down", "s")
            await _settle(pilot, app)
            assert _calls(fake_backend, "suspend_cards") == [((1001,),)]

    run_async(scenario())


def test_a_ticks_everything_and_a_second_press_clears_it(
    fake_backend: FakeBackend, run_async: Any
) -> None:
    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            await _mounted(pilot, app)
            await pilot.press("a")
            await pilot.pause()
            assert len(app._marked) == 6
            assert "6 selected" in str(app.query_one("#header-count", Static).content)
            await pilot.press("a")
            await pilot.pause()
            assert app._marked == set()

    run_async(scenario())


def test_escape_clears_the_ticks_before_it_quits(fake_backend: FakeBackend, run_async: Any) -> None:
    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            await _mounted(pilot, app)
            await pilot.press("space", "space")
            await pilot.press("escape")
            await pilot.pause()
            assert app._marked == set()
            assert app.is_running  # the first Esc only cleared the selection

    run_async(scenario())


def test_the_cursor_stays_on_its_card_after_an_action(
    fake_backend: FakeBackend, run_async: Any
) -> None:
    """Every action reloads the table; that used to send the cursor to row 0."""

    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            table = await _mounted(pilot, app)
            await pilot.press("down", "down", "s")
            await _settle(pilot, app)
            assert table.cursor_row == 2

    run_async(scenario())


def test_deleting_ticked_notes_confirms_once_for_all_of_them(
    fake_backend: FakeBackend, run_async: Any
) -> None:
    from anki_cli.tui.browse_app import ConfirmScreen

    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            await _mounted(pilot, app)
            await pilot.press("space", "space", "d")
            await _settle(pilot, app)
            assert isinstance(app.screen, ConfirmScreen)
            assert "Delete 2 notes and 2 cards?" in _screen_text(app)
            await pilot.press("y")
            await _settle(pilot, app)
            assert _calls(fake_backend, "delete_notes") == [((5000, 5001),)]

    run_async(scenario())


def test_move_prompts_for_an_existing_deck(fake_backend: FakeBackend, run_async: Any) -> None:
    from textual.widgets import Input

    from anki_cli.tui.prompts import PromptScreen

    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            await _mounted(pilot, app)
            await pilot.press("space", "space", "m")
            await _settle(pilot, app)
            assert isinstance(app.screen, PromptScreen)
            box = app.screen.query_one(Input)

            # An unknown deck keeps the prompt open and says why.
            box.value = "Nope"
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, PromptScreen)
            assert "No deck called 'Nope'" in _screen_text(app)
            assert _calls(fake_backend, "move_cards") == []

            # Matching ignores case and uses the deck's real spelling.
            box.value = "maths"
            await pilot.press("enter")
            await _settle(pilot, app)
            assert _calls(fake_backend, "move_cards") == [((1000, 1001), "Maths")]

    run_async(scenario())


def test_escape_cancels_a_prompt(fake_backend: FakeBackend, run_async: Any) -> None:
    from anki_cli.tui.prompts import PromptScreen

    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            await _mounted(pilot, app)
            await pilot.press("m")
            await _settle(pilot, app)
            assert isinstance(app.screen, PromptScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, PromptScreen)
            assert _calls(fake_backend, "move_cards") == []

    run_async(scenario())


def test_tag_prompt_adds_and_removes(fake_backend: FakeBackend, run_async: Any) -> None:
    from textual.widgets import Input

    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            await _mounted(pilot, app)
            await pilot.press("t")
            await _settle(pilot, app)
            box = app.screen.query_one(Input)

            box.value = ""
            await pilot.press("enter")
            await pilot.pause()
            assert "Type at least one tag" in _screen_text(app)

            box.value = "verb -geo"
            await pilot.press("enter")
            await _settle(pilot, app)
            assert _calls(fake_backend, "add_tags") == [((5000,), ("verb",))]
            assert _calls(fake_backend, "remove_tags") == [((5000,), ("geo",))]
            assert any("+verb -geo" in n.message for n in app._notifications)

    run_async(scenario())


def test_reschedule_prompt_takes_a_number_of_days(
    fake_backend: FakeBackend, run_async: Any
) -> None:
    from textual.widgets import Input

    async def scenario() -> None:
        app = BrowseApp(backend=fake_backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            await _mounted(pilot, app)
            await pilot.press("R")
            await _settle(pilot, app)
            box = app.screen.query_one(Input)

            for bad in ("soon", "-3", "1.5"):
                box.value = bad
                await pilot.press("enter")
                await pilot.pause()
                assert "whole number of days" in _screen_text(app), bad
            assert _calls(fake_backend, "reschedule_cards") == []

            box.value = "7"
            await pilot.press("enter")
            await _settle(pilot, app)
            assert _calls(fake_backend, "reschedule_cards") == [((1000,), 7)]

    run_async(scenario())


def test_token_suggester_completes_the_last_word_and_keeps_the_minus() -> None:
    from anki_cli.tui.prompts import TokenSuggester

    tags = TokenSuggester(["geo", "networking", "verb"])

    async def check() -> None:
        assert await tags.get_suggestion("net") == "networking"
        assert await tags.get_suggestion("geo ver") == "geo verb"
        assert await tags.get_suggestion("geo -ver") == "geo -verb"
        assert await tags.get_suggestion("verb") is None  # already complete
        assert await tags.get_suggestion("") is None
        assert await tags.get_suggestion("zzz") is None
        decks = TokenSuggester(["Languages::Japanese", "Maths"], multi=False)
        assert await decks.get_suggestion("lang") == "Languages::Japanese"
        assert await decks.get_suggestion("Languages::J") == "Languages::Japanese"

    asyncio.run(check())


def test_new_row_actions_are_unavailable_without_a_card(run_async: Any) -> None:
    backend = FakeBackend(cards=[])

    async def scenario() -> None:
        app = BrowseApp(backend=backend, query="")
        async with app.run_test(size=(130, 38)) as pilot:
            await pilot.pause()
            await pilot.pause()
            for action in (
                "toggle_mark",
                "mark_all",
                "move_selected",
                "tag_selected",
                "reschedule_selected",
            ):
                assert app.check_action(action, ()) is False, action
            await pilot.press("m", "t", "R")
            await pilot.pause()
            assert type(app.screen).__name__ == "Screen"

    run_async(scenario())
