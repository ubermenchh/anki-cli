from __future__ import annotations

import runpy

import anki_cli.tui.colors as colors


def test_main_module_invokes_cli_main(monkeypatch) -> None:
    called = {"count": 0}

    def fake_main() -> None:
        called["count"] += 1

    monkeypatch.setattr("anki_cli.cli.app.main", fake_main)
    runpy.run_module("anki_cli.__main__", run_name="__main__")

    assert called["count"] == 1


def test_text_and_fg_are_aliases() -> None:
    assert colors.TEXT == colors.FG
