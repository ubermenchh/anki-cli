from __future__ import annotations

import re
import runpy

import anki_cli.tui.colors as colors


def test_main_module_invokes_cli_main(monkeypatch) -> None:
    called = {"count": 0}

    def fake_main() -> None:
        called["count"] += 1

    monkeypatch.setattr("anki_cli.cli.app.main", fake_main)
    runpy.run_module("anki_cli.__main__", run_name="__main__")

    assert called["count"] == 1


def test_color_constants_are_hex_rgb() -> None:
    for name in (
        "BLUE",
        "CYAN",
        "GREEN",
        "RED",
        "YELLOW",
        "DIM",
        "BORDER",
        "TOOLBAR_BG",
        "MENU_BG",
        "SELECTION_BG",
        "TEXT",
        "FG",
    ):
        value = getattr(colors, name)
        assert isinstance(value, str)
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", value)


def test_text_and_fg_are_aliases() -> None:
    assert colors.TEXT == colors.FG